import os
from unittest.mock import patch

from app.config import Settings


def test_secret_with_escaped_newlines_is_normalized() -> None:
    environment = {
        "GCP_PROJECT_ID": "example-project",
        "BQ_DATASET_ID": "dataset",
        "BQ_SNAPSHOT_TABLE_ID": "snapshot",
        "BQ_RUN_TABLE_ID": "runs",
        "BQ_LOCATION": "EU",
        "GCS_BUCKET": "bucket",
        "GITHUB_ENTERPRISE_SLUG": "enterprise",
        "GITHUB_APP_ID": "123",
        "GITHUB_APP_INSTALLATION_ID": "456",
        "GITHUB_APP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----",
    }
    with patch.dict(os.environ, environment, clear=True):
        actual = Settings.from_env()

    assert "\\n" not in actual.github_app_private_key
    assert actual.github_app_private_key.count("\n") == 2

