from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is absent or invalid."""


_BQ_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9:.-]{3,127}$")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is not set")
    return value


def _positive_int(name: str, default: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None and default is not None:
        return default
    try:
        value = int(raw or "")
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _non_negative_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be numeric") from exc
    if value < 0:
        raise ConfigurationError(f"{name} cannot be negative")
    return value


def _validate_bq_identifier(name: str, value: str) -> str:
    if not _BQ_IDENTIFIER.fullmatch(value):
        raise ConfigurationError(f"{name} is not a valid BigQuery identifier")
    return value


@dataclass(frozen=True)
class Settings:
    project_id: str
    bq_dataset_id: str
    bq_snapshot_table_id: str
    bq_run_table_id: str
    bq_location: str
    gcs_bucket: str
    gcs_prefix: str
    github_api_url: str
    github_enterprise_slug: str
    github_app_id: str
    github_app_installation_id: int
    github_app_private_key: str = field(repr=False)
    github_api_version: str = "2026-03-10"
    collector_version: str = "local"
    run_mode: str = "full"
    count_delta_review_percent: float = 20.0
    http_connect_timeout_seconds: int = 10
    http_read_timeout_seconds: int = 60
    max_github_pages: int = 10_000

    @property
    def snapshot_table_fqn(self) -> str:
        return (
            f"{self.project_id}.{self.bq_dataset_id}."
            f"{self.bq_snapshot_table_id}"
        )

    @property
    def run_table_fqn(self) -> str:
        return f"{self.project_id}.{self.bq_dataset_id}.{self.bq_run_table_id}"

    @classmethod
    def from_env(cls) -> "Settings":
        project_id = (
            os.getenv("GCP_PROJECT_ID")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
            or ""
        ).strip()
        if not _PROJECT_ID.fullmatch(project_id):
            raise ConfigurationError("GCP_PROJECT_ID is missing or invalid")

        api_url = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        if not api_url.startswith("https://"):
            raise ConfigurationError("GITHUB_API_URL must use HTTPS")

        private_key = _required("GITHUB_APP_PRIVATE_KEY")
        # Some secret-loading paths preserve escaped newlines rather than PEM
        # newlines. Support both without logging the secret.
        if "\\n" in private_key and "\n" not in private_key:
            private_key = private_key.replace("\\n", "\n")

        run_mode = os.getenv("RUN_MODE", "full").strip().lower()
        if run_mode not in {"full", "validate"}:
            raise ConfigurationError("RUN_MODE must be either full or validate")

        return cls(
            project_id=project_id,
            bq_dataset_id=_validate_bq_identifier(
                "BQ_DATASET_ID", _required("BQ_DATASET_ID")
            ),
            bq_snapshot_table_id=_validate_bq_identifier(
                "BQ_SNAPSHOT_TABLE_ID", _required("BQ_SNAPSHOT_TABLE_ID")
            ),
            bq_run_table_id=_validate_bq_identifier(
                "BQ_RUN_TABLE_ID", _required("BQ_RUN_TABLE_ID")
            ),
            bq_location=_required("BQ_LOCATION"),
            gcs_bucket=_required("GCS_BUCKET"),
            gcs_prefix=os.getenv(
                "GCS_PREFIX", "github-license-snapshots"
            ).strip("/"),
            github_api_url=api_url,
            github_enterprise_slug=_required("GITHUB_ENTERPRISE_SLUG"),
            github_app_id=_required("GITHUB_APP_ID"),
            github_app_installation_id=_positive_int(
                "GITHUB_APP_INSTALLATION_ID"
            ),
            github_app_private_key=private_key,
            github_api_version=os.getenv(
                "GITHUB_API_VERSION", "2026-03-10"
            ).strip(),
            collector_version=os.getenv("COLLECTOR_VERSION", "local").strip(),
            run_mode=run_mode,
            count_delta_review_percent=_non_negative_float(
                "COUNT_DELTA_REVIEW_PERCENT", 20.0
            ),
            http_connect_timeout_seconds=_positive_int(
                "HTTP_CONNECT_TIMEOUT_SECONDS", 10
            ),
            http_read_timeout_seconds=_positive_int(
                "HTTP_READ_TIMEOUT_SECONDS", 60
            ),
            max_github_pages=_positive_int("MAX_GITHUB_PAGES", 10_000),
        )
