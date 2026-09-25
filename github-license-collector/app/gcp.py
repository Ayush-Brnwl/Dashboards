from __future__ import annotations

import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from google.api_core.exceptions import Conflict, PreconditionFailed
from google.cloud import bigquery, storage

from app.config import Settings


SOURCE_SYSTEM = "GITHUB_CONSUMED_LICENSES_API"
TERMINAL_STATUSES = frozenset(
    {"COMPLETE", "REVIEW_REQUIRED", "REJECTED"}
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _gzip(value: bytes) -> bytes:
    # mtime=0 makes retries byte-for-byte deterministic.
    return gzip.compress(value, compresslevel=9, mtime=0)


def _utc_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def safe_error_message(exc: BaseException, limit: int = 2000) -> str:
    """Return a single-line operational error without exception chaining."""
    message = " ".join(str(exc).replace("\x00", "").split())
    return (message or exc.__class__.__name__)[:limit]


@dataclass(frozen=True)
class ArchiveResult:
    canonical_uri: str
    manifest_uri: str
    raw_page_uris: tuple[str, ...]


class GcsArchive:
    """Writes immutable, execution-scoped evidence objects to Cloud Storage."""

    def __init__(
        self,
        settings: Settings,
        client: storage.Client | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or storage.Client(project=settings.project_id)
        self._bucket = self._client.bucket(settings.gcs_bucket)

    def archive(
        self,
        *,
        snapshot_date: date,
        snapshot_run_id: str,
        raw_pages: list[dict[str, Any]],
        canonical_ndjson: bytes,
        manifest: dict[str, Any],
    ) -> ArchiveResult:
        root = (
            f"{self._settings.gcs_prefix}/snapshot_date={snapshot_date.isoformat()}"
            f"/run_id={snapshot_run_id}"
        )
        raw_uris: list[str] = []
        for page_number, page in enumerate(raw_pages, start=1):
            name = f"{root}/raw/page-{page_number:05d}.json.gz"
            raw_uris.append(
                self._upload_immutable(name, _gzip(_json_bytes(page)))
            )

        canonical_name = f"{root}/canonical/users.ndjson.gz"
        canonical_uri = self._upload_immutable(
            canonical_name,
            _gzip(canonical_ndjson),
        )
        manifest_name = f"{root}/manifest/extracted.json.gz"
        manifest_uri = self._upload_immutable(
            manifest_name,
            _gzip(_json_bytes(manifest)),
        )
        return ArchiveResult(
            canonical_uri=canonical_uri,
            manifest_uri=manifest_uri,
            raw_page_uris=tuple(raw_uris),
        )

    def write_validation_manifest(
        self,
        *,
        snapshot_date: date,
        snapshot_run_id: str,
        manifest: dict[str, Any],
    ) -> str:
        name = (
            f"{self._settings.gcs_prefix}/snapshot_date={snapshot_date.isoformat()}"
            f"/run_id={snapshot_run_id}/manifest/validated.json.gz"
        )
        return self._upload_immutable(name, _gzip(_json_bytes(manifest)))

    def _upload_immutable(self, name: str, value: bytes) -> str:
        digest = hashlib.sha256(value).hexdigest()
        blob = self._bucket.blob(name)
        blob.metadata = {"sha256": digest}
        try:
            blob.upload_from_string(
                value,
                content_type="application/gzip",
                if_generation_match=0,
                checksum="crc32c",
            )
        except PreconditionFailed:
            # The same Cloud Run execution may be retried. Accept an existing
            # object only when it has exactly the expected content digest.
            blob.reload()
            existing_digest = (blob.metadata or {}).get("sha256")
            if existing_digest != digest:
                raise RuntimeError(
                    f"Immutable archive object gs://{self._bucket.name}/{name} "
                    "already exists with different content"
                )
        return f"gs://{self._bucket.name}/{name}"


@dataclass(frozen=True)
class SnapshotValidation:
    loaded_record_count: int
    distinct_identity_count: int
    duplicate_identity_count: int
    missing_github_login_count: int
    unresolved_identity_count: int
    previous_complete_record_count: int | None

    def count_delta_percent(self) -> float | None:
        previous = self.previous_complete_record_count
        if previous is None or previous == 0:
            return None
        return abs(self.loaded_record_count - previous) * 100.0 / previous


@dataclass(frozen=True)
class RunState:
    status: str
    snapshot_date: date
    collection_started_at: datetime


class BigQueryRepository:
    """DML and load operations against Terraform-managed BigQuery objects."""

    def __init__(
        self,
        settings: Settings,
        client: bigquery.Client | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or bigquery.Client(
            project=settings.project_id,
            location=settings.bq_location,
        )

    def prepare_run(
        self,
        *,
        snapshot_run_id: str,
        snapshot_date: date,
        started_at: datetime,
    ) -> RunState:
        # A task retry can cross midnight UTC. Search the bounded current/ prior
        # date partitions first so the execution retains its original date and
        # timestamp. The bounded predicate also satisfies requirePartitionFilter.
        existing_query = f"""
        SELECT status, snapshot_date, collection_started_at
        FROM `{self._settings.run_table_fqn}`
        WHERE snapshot_date BETWEEN DATE_SUB(@snapshot_date, INTERVAL 1 DAY)
                                AND @snapshot_date
          AND snapshot_run_id = @snapshot_run_id
        ORDER BY snapshot_date DESC
        LIMIT 1
        """
        existing_rows = list(
            self._query(
                existing_query,
                [
                    self._date("snapshot_date", snapshot_date),
                    self._string("snapshot_run_id", snapshot_run_id),
                ],
            )
        )
        if existing_rows:
            row = existing_rows[0]
            existing_date = row["snapshot_date"]
            existing_started_at = row["collection_started_at"]
            existing_status = str(row["status"])
            if existing_status == "FAILED":
                self._update_status(
                    snapshot_run_id=snapshot_run_id,
                    snapshot_date=existing_date,
                    status="STARTED",
                    completed_at=None,
                    failure_stage=None,
                    error_message=None,
                )
                existing_status = "STARTED"
            return RunState(
                status=existing_status,
                snapshot_date=existing_date,
                collection_started_at=existing_started_at,
            )

        query = f"""
        MERGE `{self._settings.run_table_fqn}` AS target
        USING (
          SELECT
            @snapshot_run_id AS snapshot_run_id,
            @enterprise_slug AS enterprise_slug,
            @snapshot_date AS snapshot_date,
            @started_at AS collection_started_at
        ) AS source
        ON target.snapshot_run_id = source.snapshot_run_id
           AND target.snapshot_date = source.snapshot_date
        WHEN NOT MATCHED THEN
          INSERT (
            snapshot_run_id,
            enterprise_slug,
            snapshot_date,
            collection_started_at,
            status,
            source_system,
            source_api_version,
            collector_version,
            created_at,
            updated_at
          )
          VALUES (
            source.snapshot_run_id,
            source.enterprise_slug,
            source.snapshot_date,
            source.collection_started_at,
            'STARTED',
            @source_system,
            @source_api_version,
            @collector_version,
            @started_at,
            @started_at
          )
        """
        self._query(
            query,
            [
                self._string("snapshot_run_id", snapshot_run_id),
                self._string(
                    "enterprise_slug", self._settings.github_enterprise_slug
                ),
                self._date("snapshot_date", snapshot_date),
                self._timestamp("started_at", started_at),
                self._string("source_system", SOURCE_SYSTEM),
                self._string(
                    "source_api_version", self._settings.github_api_version
                ),
                self._string(
                    "collector_version", self._settings.collector_version
                ),
            ],
        )

        status = self.get_status(snapshot_run_id, snapshot_date)
        return RunState(
            status=status,
            snapshot_date=snapshot_date,
            collection_started_at=started_at,
        )

    def get_status(self, snapshot_run_id: str, snapshot_date: date) -> str:
        query = f"""
        SELECT status
        FROM `{self._settings.run_table_fqn}`
        WHERE snapshot_date = @snapshot_date
          AND snapshot_run_id = @snapshot_run_id
        LIMIT 1
        """
        rows = list(
            self._query(
                query,
                [
                    self._date("snapshot_date", snapshot_date),
                    self._string("snapshot_run_id", snapshot_run_id),
                ],
            )
        )
        if not rows:
            raise RuntimeError("Snapshot run row was not created")
        return str(rows[0]["status"])

    def record_extraction(
        self,
        *,
        snapshot_run_id: str,
        snapshot_date: date,
        api_total_seats_consumed: int | None,
        api_total_seats_purchased: int | None,
        api_user_record_count: int,
        pages_fetched: int,
        pagination_complete: bool,
        duplicate_identity_count: int,
        missing_github_login_count: int,
        unresolved_identity_count: int,
        updated_at: datetime,
    ) -> None:
        query = f"""
        UPDATE `{self._settings.run_table_fqn}`
        SET status = 'EXTRACTED',
            api_total_seats_consumed = @api_total_seats_consumed,
            api_total_seats_purchased = @api_total_seats_purchased,
            api_user_record_count = @api_user_record_count,
            pages_fetched = @pages_fetched,
            pagination_complete = @pagination_complete,
            duplicate_identity_count = @duplicate_identity_count,
            missing_github_login_count = @missing_github_login_count,
            unresolved_identity_count = @unresolved_identity_count,
            failure_stage = NULL,
            error_message = NULL,
            updated_at = @updated_at
        WHERE snapshot_date = @snapshot_date
          AND snapshot_run_id = @snapshot_run_id
        """
        self._query(
            query,
            [
                self._int("api_total_seats_consumed", api_total_seats_consumed),
                self._int("api_total_seats_purchased", api_total_seats_purchased),
                self._int("api_user_record_count", api_user_record_count),
                self._int("pages_fetched", pages_fetched),
                self._bool("pagination_complete", pagination_complete),
                self._int("duplicate_identity_count", duplicate_identity_count),
                self._int(
                    "missing_github_login_count", missing_github_login_count
                ),
                self._int("unresolved_identity_count", unresolved_identity_count),
                self._timestamp("updated_at", updated_at),
                self._date("snapshot_date", snapshot_date),
                self._string("snapshot_run_id", snapshot_run_id),
            ],
        )

    def load_snapshot(
        self,
        *,
        snapshot_run_id: str,
        snapshot_date: date,
        source_uri: str,
    ) -> int:
        destination = self._client.get_table(
            self._settings.snapshot_table_fqn
        )
        config = bigquery.LoadJobConfig(
            schema=destination.schema,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            compression=bigquery.Compression.GZIP,
            create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            autodetect=False,
            ignore_unknown_values=False,
            max_bad_records=0,
        )
        job_id = self._load_job_id(snapshot_run_id)
        try:
            job = self._client.load_table_from_uri(
                source_uri,
                self._settings.snapshot_table_fqn,
                job_id=job_id,
                location=self._settings.bq_location,
                job_config=config,
            )
        except Conflict:
            job = self._client.get_job(
                job_id,
                location=self._settings.bq_location,
            )
        job.result()
        if job.errors:
            raise RuntimeError(f"BigQuery load job failed: {job.errors!r}")

        output_rows = int(job.output_rows or 0)
        query = f"""
        UPDATE `{self._settings.run_table_fqn}`
        SET status = 'LOADED',
            loaded_user_record_count = @loaded_user_record_count,
            updated_at = CURRENT_TIMESTAMP()
        WHERE snapshot_date = @snapshot_date
          AND snapshot_run_id = @snapshot_run_id
        """
        self._query(
            query,
            [
                self._int("loaded_user_record_count", output_rows),
                self._date("snapshot_date", snapshot_date),
                self._string("snapshot_run_id", snapshot_run_id),
            ],
        )
        return output_rows

    def validate_snapshot(
        self,
        *,
        snapshot_run_id: str,
        snapshot_date: date,
    ) -> SnapshotValidation:
        query = f"""
        SELECT
          COUNT(*) AS loaded_record_count,
          COUNT(DISTINCT license_identity_key) AS distinct_identity_count,
          COUNT(*) - COUNT(DISTINCT license_identity_key)
            AS duplicate_identity_count,
          COUNTIF(github_login_normalized IS NULL)
            AS missing_github_login_count,
          COUNTIF(identity_key_type = 'UNRESOLVED')
            AS unresolved_identity_count
        FROM `{self._settings.snapshot_table_fqn}`
        WHERE snapshot_date = @snapshot_date
          AND snapshot_run_id = @snapshot_run_id
        """
        row = list(
            self._query(
                query,
                [
                    self._date("snapshot_date", snapshot_date),
                    self._string("snapshot_run_id", snapshot_run_id),
                ],
            )
        )[0]

        previous_query = f"""
        SELECT api_user_record_count
        FROM `{self._settings.run_table_fqn}`
        WHERE snapshot_date <= @snapshot_date
          AND enterprise_slug = @enterprise_slug
          AND status = 'COMPLETE'
          AND snapshot_run_id != @snapshot_run_id
          AND api_user_record_count IS NOT NULL
        ORDER BY collection_completed_at DESC
        LIMIT 1
        """
        previous_rows = list(
            self._query(
                previous_query,
                [
                    self._date("snapshot_date", snapshot_date),
                    self._string(
                        "enterprise_slug",
                        self._settings.github_enterprise_slug,
                    ),
                    self._string("snapshot_run_id", snapshot_run_id),
                ],
            )
        )
        previous = (
            int(previous_rows[0]["api_user_record_count"])
            if previous_rows
            else None
        )
        return SnapshotValidation(
            loaded_record_count=int(row["loaded_record_count"]),
            distinct_identity_count=int(row["distinct_identity_count"]),
            duplicate_identity_count=int(row["duplicate_identity_count"]),
            missing_github_login_count=int(row["missing_github_login_count"]),
            unresolved_identity_count=int(row["unresolved_identity_count"]),
            previous_complete_record_count=previous,
        )

    def finish_run(
        self,
        *,
        snapshot_run_id: str,
        snapshot_date: date,
        status: str,
        loaded_user_record_count: int,
        duplicate_identity_count: int,
        missing_github_login_count: int,
        unresolved_identity_count: int,
        reason: str | None,
        completed_at: datetime,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"Unsupported terminal status: {status}")
        self._update_status(
            snapshot_run_id=snapshot_run_id,
            snapshot_date=snapshot_date,
            status=status,
            completed_at=completed_at,
            failure_stage="VALIDATION" if reason else None,
            error_message=reason,
            loaded_user_record_count=loaded_user_record_count,
            duplicate_identity_count=duplicate_identity_count,
            missing_github_login_count=missing_github_login_count,
            unresolved_identity_count=unresolved_identity_count,
        )

    def mark_failed(
        self,
        *,
        snapshot_run_id: str,
        snapshot_date: date,
        failure_stage: str,
        error_message: str,
        completed_at: datetime,
    ) -> None:
        self._update_status(
            snapshot_run_id=snapshot_run_id,
            snapshot_date=snapshot_date,
            status="FAILED",
            completed_at=completed_at,
            failure_stage=failure_stage[:100],
            error_message=error_message,
        )

    def _update_status(
        self,
        *,
        snapshot_run_id: str,
        snapshot_date: date,
        status: str,
        completed_at: datetime | None,
        failure_stage: str | None,
        error_message: str | None,
        loaded_user_record_count: int | None = None,
        duplicate_identity_count: int | None = None,
        missing_github_login_count: int | None = None,
        unresolved_identity_count: int | None = None,
    ) -> None:
        query = f"""
        UPDATE `{self._settings.run_table_fqn}`
        SET status = @status,
            collection_completed_at = @completed_at,
            failure_stage = @failure_stage,
            error_message = @error_message,
            loaded_user_record_count = COALESCE(
              @loaded_user_record_count, loaded_user_record_count
            ),
            duplicate_identity_count = COALESCE(
              @duplicate_identity_count, duplicate_identity_count
            ),
            missing_github_login_count = COALESCE(
              @missing_github_login_count, missing_github_login_count
            ),
            unresolved_identity_count = COALESCE(
              @unresolved_identity_count, unresolved_identity_count
            ),
            updated_at = CURRENT_TIMESTAMP()
        WHERE snapshot_date = @snapshot_date
          AND snapshot_run_id = @snapshot_run_id
        """
        self._query(
            query,
            [
                self._string("status", status),
                self._timestamp("completed_at", completed_at),
                self._string("failure_stage", failure_stage),
                self._string("error_message", error_message),
                self._int(
                    "loaded_user_record_count", loaded_user_record_count
                ),
                self._int(
                    "duplicate_identity_count", duplicate_identity_count
                ),
                self._int(
                    "missing_github_login_count", missing_github_login_count
                ),
                self._int(
                    "unresolved_identity_count", unresolved_identity_count
                ),
                self._date("snapshot_date", snapshot_date),
                self._string("snapshot_run_id", snapshot_run_id),
            ],
        )

    def _query(
        self,
        query: str,
        parameters: list[bigquery.ScalarQueryParameter],
    ) -> bigquery.table.RowIterator:
        config = bigquery.QueryJobConfig(query_parameters=parameters)
        return self._client.query(
            query,
            job_config=config,
            location=self._settings.bq_location,
        ).result()

    @staticmethod
    def _load_job_id(snapshot_run_id: str) -> str:
        rendered = re.sub(r"[^A-Za-z0-9_-]", "_", snapshot_run_id)
        digest = hashlib.sha256(snapshot_run_id.encode("utf-8")).hexdigest()[:12]
        return f"github_license_snapshot_{rendered[:700]}_{digest}"

    @staticmethod
    def _string(name: str, value: str | None) -> bigquery.ScalarQueryParameter:
        return bigquery.ScalarQueryParameter(name, "STRING", value)

    @staticmethod
    def _int(name: str, value: int | None) -> bigquery.ScalarQueryParameter:
        return bigquery.ScalarQueryParameter(name, "INT64", value)

    @staticmethod
    def _bool(name: str, value: bool | None) -> bigquery.ScalarQueryParameter:
        return bigquery.ScalarQueryParameter(name, "BOOL", value)

    @staticmethod
    def _date(name: str, value: date) -> bigquery.ScalarQueryParameter:
        return bigquery.ScalarQueryParameter(name, "DATE", value)

    @staticmethod
    def _timestamp(
        name: str, value: datetime | None
    ) -> bigquery.ScalarQueryParameter:
        return bigquery.ScalarQueryParameter(name, "TIMESTAMP", value)
