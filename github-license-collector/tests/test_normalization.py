from datetime import date, datetime, timezone

from app.normalization import (
    NormalizationContext,
    normalize_license_user,
    serialize_ndjson,
)


CONTEXT = NormalizationContext(
    snapshot_date=date(2026, 9, 25),
    snapshot_run_id="collector-abc123",
    captured_at=datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc),
    enterprise_slug="bank-enterprise",
    api_version="2026-03-10",
    collector_version="test",
)


def test_normalizes_github_com_user() -> None:
    source = {
        "github_com_login": "MonaLisa",
        "github_com_name": "Mona Lisa",
        "github_com_user": True,
        "enterprise_server_user": False,
        "visual_studio_subscription_user": False,
        "license_type": "enterprise",
        "github_com_profile": "https://github.com/MonaLisa",
        "github_com_member_roles": ["org-b:Member", "org-a:Owner"],
        "github_com_enterprise_roles": ["member"],
        "github_com_verified_domain_emails": ["MONA@EXAMPLE.COM"],
        "github_com_saml_name_id": "mona@example.com",
        "github_com_orgs_with_pending_invites": [],
        "github_com_two_factor_auth": True,
        "enterprise_server_user_ids": [],
        "enterprise_server_emails": [],
        "visual_studio_license_status": "",
        "visual_studio_subscription_email": "",
        "total_user_accounts": 1,
    }

    actual = normalize_license_user(source, CONTEXT)

    assert actual["license_identity_key"] == "github:monalisa"
    assert actual["identity_key_type"] == "GITHUB_LOGIN"
    assert actual["github_login"] == "MonaLisa"
    assert actual["github_login_normalized"] == "monalisa"
    assert actual["email_address"] == "mona@example.com"
    assert actual["verified_domain_emails"] == ["mona@example.com"]
    assert actual["member_roles"] == ["org-a:Owner", "org-b:Member"]
    assert actual["loaded_at"] == "2026-09-25T02:00:00.000000Z"
    assert len(actual["source_record_hash"]) == 64


def test_falls_back_to_server_identity_without_github_login() -> None:
    source = {
        "github_com_login": "",
        "github_com_saml_name_id": "",
        "enterprise_server_user_ids": ["ghe.example:123"],
        "enterprise_server_emails": ["SERVER.USER@EXAMPLE.COM"],
    }

    actual = normalize_license_user(source, CONTEXT)

    assert actual["identity_key_type"] == "SERVER_USER_IDS"
    assert actual["license_identity_key"].startswith("server:")
    assert actual["github_login_normalized"] is None
    assert actual["email_address"] == "server.user@example.com"


def test_unresolved_identity_is_stable_and_ndjson_is_deterministic() -> None:
    first = normalize_license_user({}, CONTEXT)
    second = normalize_license_user({}, CONTEXT)

    assert first == second
    assert first["identity_key_type"] == "UNRESOLVED"
    assert serialize_ndjson([first]) == serialize_ndjson([second])

