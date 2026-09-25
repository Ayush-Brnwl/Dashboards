from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.gcp import (
    BigQueryRepository,
    GcsArchive,
    TERMINAL_STATUSES,
    safe_error_message,
)
from app.github_client import GitHubLicenseClient
from app.normalization import (
    NormalizationContext,
    normalize_license_user,
    serialize_ndjson,
)


LOGGER = logging.getLogger("github-license-collector")


def _log(level: int, event: str, **fields: Any) -> None:
    # Values are deliberately limited to operational metadata. Never add a
    # token, private key, email address, login, or raw API body here.
    payload = {"event": event, **fields}
    LOGGER.log(
        level,
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )


def _run_id() -> str:
    candidate = os.getenv("CLOUD_RUN_EXECUTION", "").strip()
    if not candidate:
        candidate = f"manual-{uuid.uuid4()}"
    candidate = re.sub(r"[^A-Za-z0-9_-]", "_", candidate)[:200]
    if not candidate:
        raise RuntimeError("Unable to derive a safe snapshot run ID")
    return candidate


def _local_metrics(records: list[dict[str, Any]]) -> dict[str, int]:
    identity_counts = Counter(
        str(record["license_identity_key"]) for record in records
    )
    return {
        "duplicate_identity_count": sum(
            count - 1 for count in identity_counts.values() if count > 1
        ),
        "missing_github_login_count": sum(
            1
            for record in records
            if record.get("github_login_normalized") is None
        ),
        "unresolved_identity_count": sum(
            1
            for record in records
            if record.get("identity_key_type") == "UNRESOLVED"
        ),
    }


def _review_reasons(
    *,
    api_total_seats_consumed: int | None,
    api_user_record_count: int,
    loaded_record_count: int,
    duplicate_identity_count: int,
    unresolved_identity_count: int,
    count_delta_percent: float | None,
    count_delta_review_percent: float,
) -> list[str]:
    reasons: list[str] = []
    if loaded_record_count != api_user_record_count:
        reasons.append(
            "Loaded record count does not equal the API user record count"
        )
    if duplicate_identity_count:
        reasons.append(
            f"Snapshot contains {duplicate_identity_count} duplicate identities"
        )
    if unresolved_identity_count:
        reasons.append(
            f"Snapshot contains {unresolved_identity_count} unresolved identities"
        )
    if (
        api_total_seats_consumed is not None
        and api_total_seats_consumed != api_user_record_count
    ):
        reasons.append(
            "API total_seats_consumed does not equal the paginated user count"
        )
    if (
        count_delta_percent is not None
        and count_delta_percent > count_delta_review_percent
    ):
        reasons.append(
            "User count changed by "
            f"{count_delta_percent:.2f}% versus the previous complete snapshot"
        )
    return reasons


def run() -> int:
    settings = Settings.from_env()
    snapshot_run_id = _run_id()
    started_at = datetime.now(timezone.utc)
    snapshot_date = started_at.date()

    _log(
        logging.INFO,
        "collector_started",
        snapshot_run_id=snapshot_run_id,
        snapshot_date=snapshot_date.isoformat(),
        run_mode=settings.run_mode,
        collector_version=settings.collector_version,
    )

    github = GitHubLicenseClient(settings)
    stage = "GITHUB_AUTHENTICATION"
    repository: BigQueryRepository | None = None

    try:
        if settings.run_mode == "full":
            stage = "RUN_INITIALIZATION"
            repository = BigQueryRepository(settings)
            run_state = repository.prepare_run(
                snapshot_run_id=snapshot_run_id,
                snapshot_date=snapshot_date,
                started_at=started_at,
            )
            current_status = run_state.status
            snapshot_date = run_state.snapshot_date
            started_at = run_state.collection_started_at
            if current_status in TERMINAL_STATUSES:
                _log(
                    logging.INFO,
                    "terminal_run_already_exists",
                    snapshot_run_id=snapshot_run_id,
                    status=current_status,
                )
                return 2 if current_status == "REJECTED" else 0

        stage = "GITHUB_AUTHENTICATION"
        installation_token = github.create_installation_token()
        stage = "GITHUB_EXTRACTION"
        fetched = github.fetch_consumed_licenses(installation_token)
        # Drop the last local reference as soon as extraction is complete.
        del installation_token

        stage = "NORMALIZATION"
        context = NormalizationContext(
            snapshot_date=snapshot_date,
            snapshot_run_id=snapshot_run_id,
            captured_at=started_at,
            enterprise_slug=settings.github_enterprise_slug,
            api_version=settings.github_api_version,
            collector_version=settings.collector_version,
        )
        records = [
            normalize_license_user(user, context) for user in fetched.users
        ]
        ndjson = serialize_ndjson(records) if records else b""
        local_metrics = _local_metrics(records)

        _log(
            logging.INFO,
            "github_extraction_completed",
            snapshot_run_id=snapshot_run_id,
            pages_fetched=fetched.pages_fetched,
            api_user_record_count=len(fetched.users),
            api_total_seats_consumed=fetched.total_seats_consumed,
            api_total_seats_purchased=fetched.total_seats_purchased,
            rate_limit_remaining=fetched.rate_limit_remaining,
            **local_metrics,
        )

        if settings.run_mode == "validate":
            if not records:
                raise RuntimeError("GitHub returned no consumed-license records")
            _log(
                logging.INFO,
                "validation_mode_succeeded",
                snapshot_run_id=snapshot_run_id,
                api_user_record_count=len(records),
            )
            return 0

        if repository is None:  # Defensive; impossible after full-mode setup.
            raise RuntimeError("BigQuery repository was not initialized")

        stage = "GCS_ARCHIVE"
        archive = GcsArchive(settings)
        extracted_manifest = {
            "schema_version": 1,
            "snapshot_run_id": snapshot_run_id,
            "snapshot_date": snapshot_date.isoformat(),
            "enterprise_slug": settings.github_enterprise_slug,
            "captured_at": started_at.isoformat(),
            "source_system": "GITHUB_CONSUMED_LICENSES_API",
            "source_api_version": settings.github_api_version,
            "collector_version": settings.collector_version,
            "pages_fetched": fetched.pages_fetched,
            "pagination_complete": fetched.pagination_complete,
            "api_user_record_count": len(records),
            "api_total_seats_consumed": fetched.total_seats_consumed,
            "api_total_seats_purchased": fetched.total_seats_purchased,
            "canonical_sha256": hashlib.sha256(ndjson).hexdigest(),
            **local_metrics,
        }
        archived = archive.archive(
            snapshot_date=snapshot_date,
            snapshot_run_id=snapshot_run_id,
            raw_pages=fetched.raw_pages,
            canonical_ndjson=ndjson,
            manifest=extracted_manifest,
        )

        stage = "RUN_METADATA_UPDATE"
        repository.record_extraction(
            snapshot_run_id=snapshot_run_id,
            snapshot_date=snapshot_date,
            api_total_seats_consumed=fetched.total_seats_consumed,
            api_total_seats_purchased=fetched.total_seats_purchased,
            api_user_record_count=len(records),
            pages_fetched=fetched.pages_fetched,
            pagination_complete=fetched.pagination_complete,
            updated_at=datetime.now(timezone.utc),
            **local_metrics,
        )

        if not records:
            reason = "GitHub returned no consumed-license records"
            repository.finish_run(
                snapshot_run_id=snapshot_run_id,
                snapshot_date=snapshot_date,
                status="REJECTED",
                loaded_user_record_count=0,
                completed_at=datetime.now(timezone.utc),
                reason=reason,
                **local_metrics,
            )
            _log(
                logging.ERROR,
                "snapshot_rejected",
                snapshot_run_id=snapshot_run_id,
                reason=reason,
            )
            return 2

        stage = "BIGQUERY_LOAD"
        repository.load_snapshot(
            snapshot_run_id=snapshot_run_id,
            snapshot_date=snapshot_date,
            source_uri=archived.canonical_uri,
        )

        stage = "BIGQUERY_VALIDATION"
        validation = repository.validate_snapshot(
            snapshot_run_id=snapshot_run_id,
            snapshot_date=snapshot_date,
        )
        count_delta = validation.count_delta_percent()
        reasons = _review_reasons(
            api_total_seats_consumed=fetched.total_seats_consumed,
            api_user_record_count=len(records),
            loaded_record_count=validation.loaded_record_count,
            duplicate_identity_count=validation.duplicate_identity_count,
            unresolved_identity_count=validation.unresolved_identity_count,
            count_delta_percent=count_delta,
            count_delta_review_percent=settings.count_delta_review_percent,
        )

        hard_rejection = (
            validation.loaded_record_count != len(records)
            or not fetched.pagination_complete
        )
        if hard_rejection:
            terminal_status = "REJECTED"
        elif reasons:
            terminal_status = "REVIEW_REQUIRED"
        else:
            terminal_status = "COMPLETE"
        reason = "; ".join(reasons)[:2000] if reasons else None

        stage = "VALIDATION_MANIFEST"
        archive.write_validation_manifest(
            snapshot_date=snapshot_date,
            snapshot_run_id=snapshot_run_id,
            manifest={
                "schema_version": 1,
                "snapshot_run_id": snapshot_run_id,
                "snapshot_date": snapshot_date.isoformat(),
                "run_started_at": started_at.isoformat(),
                "status": terminal_status,
                "api_user_record_count": len(records),
                "loaded_record_count": validation.loaded_record_count,
                "distinct_identity_count": validation.distinct_identity_count,
                "duplicate_identity_count": (
                    validation.duplicate_identity_count
                ),
                "missing_github_login_count": (
                    validation.missing_github_login_count
                ),
                "unresolved_identity_count": (
                    validation.unresolved_identity_count
                ),
                "previous_complete_record_count": (
                    validation.previous_complete_record_count
                ),
                "count_delta_percent": count_delta,
                "review_reasons": reasons,
            },
        )

        stage = "RUN_FINALIZATION"
        repository.finish_run(
            snapshot_run_id=snapshot_run_id,
            snapshot_date=snapshot_date,
            status=terminal_status,
            loaded_user_record_count=validation.loaded_record_count,
            duplicate_identity_count=validation.duplicate_identity_count,
            missing_github_login_count=(
                validation.missing_github_login_count
            ),
            unresolved_identity_count=validation.unresolved_identity_count,
            reason=reason,
            completed_at=datetime.now(timezone.utc),
        )
        _log(
            logging.INFO if terminal_status == "COMPLETE" else logging.WARNING,
            "collector_finished",
            snapshot_run_id=snapshot_run_id,
            status=terminal_status,
            loaded_record_count=validation.loaded_record_count,
            count_delta_percent=count_delta,
            review_reason_count=len(reasons),
        )
        return 2 if terminal_status == "REJECTED" else 0

    except Exception as exc:
        message = safe_error_message(exc)
        _log(
            logging.ERROR,
            "collector_failed",
            snapshot_run_id=snapshot_run_id,
            failure_stage=stage,
            error_type=exc.__class__.__name__,
            error_message=message,
        )
        if repository is not None:
            try:
                repository.mark_failed(
                    snapshot_run_id=snapshot_run_id,
                    snapshot_date=snapshot_date,
                    failure_stage=stage,
                    error_message=message,
                    completed_at=datetime.now(timezone.utc),
                )
            except Exception as metadata_exc:
                _log(
                    logging.ERROR,
                    "failure_metadata_update_failed",
                    snapshot_run_id=snapshot_run_id,
                    error_type=metadata_exc.__class__.__name__,
                    error_message=safe_error_message(metadata_exc),
                )
        raise


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(message)s",
        stream=sys.stdout,
    )
    raise SystemExit(run())


if __name__ == "__main__":
    main()
