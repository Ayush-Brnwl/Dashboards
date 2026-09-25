from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any


def _text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _normalized_text(value: Any) -> str | None:
    rendered = _text(value)
    return rendered.lower() if rendered else None


def _string_list(value: Any, *, lowercase: bool = False) -> list[str]:
    if value is None:
        return []
    source = value if isinstance(value, list) else [value]
    output: set[str] = set()
    for item in source:
        rendered = _text(item)
        if rendered:
            output.add(rendered.lower() if lowercase else rendered)
    return sorted(output)


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    rendered = str(value).strip().lower()
    if rendered in {"1", "true", "yes", "y", "enabled"}:
        return True
    if rendered in {"0", "false", "no", "n", "disabled"}:
        return False
    return None


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class NormalizationContext:
    snapshot_date: date
    snapshot_run_id: str
    captured_at: datetime
    enterprise_slug: str
    api_version: str
    collector_version: str


def normalize_license_user(
    source: dict[str, Any], context: NormalizationContext
) -> dict[str, Any]:
    login = _text(source.get("github_com_login"))
    normalized_login = _normalized_text(login)
    saml_name_id = _text(source.get("github_com_saml_name_id"))
    normalized_saml = _normalized_text(saml_name_id)
    server_user_ids = _string_list(source.get("enterprise_server_user_ids"))

    source_json = _stable_json(source)
    source_hash = _sha256(source_json)

    if normalized_login:
        identity_key = f"github:{normalized_login}"
        identity_key_type = "GITHUB_LOGIN"
    elif normalized_saml:
        identity_key = f"saml:{normalized_saml}"
        identity_key_type = "SAML_NAME_ID"
    elif server_user_ids:
        identity_key = f"server:{_sha256('|'.join(server_user_ids))}"
        identity_key_type = "SERVER_USER_IDS"
    else:
        identity_key = f"unresolved:{source_hash}"
        identity_key_type = "UNRESOLVED"

    verified_emails = _string_list(
        source.get("github_com_verified_domain_emails"), lowercase=True
    )
    server_emails = _string_list(
        source.get("enterprise_server_emails"), lowercase=True
    )
    visual_studio_email = _normalized_text(
        source.get("visual_studio_subscription_email")
    )

    if verified_emails:
        email_address = verified_emails[0]
    elif normalized_saml and "@" in normalized_saml:
        email_address = normalized_saml
    elif server_emails:
        email_address = server_emails[0]
    else:
        email_address = visual_studio_email

    enterprise_roles = _string_list(
        source.get("github_com_enterprise_roles")
    )
    if not enterprise_roles:
        enterprise_roles = _string_list(
            source.get("github_com_enterprise_role")
        )

    return {
        "snapshot_date": context.snapshot_date.isoformat(),
        "snapshot_run_id": context.snapshot_run_id,
        "captured_at": _utc_timestamp(context.captured_at),
        "enterprise_slug": context.enterprise_slug,
        "license_identity_key": identity_key,
        "identity_key_type": identity_key_type,
        "github_login": login,
        "github_login_normalized": normalized_login,
        "github_name": _text(source.get("github_com_name")),
        "github_profile_url": _text(source.get("github_com_profile")),
        "email_address": email_address,
        "verified_domain_emails": verified_emails,
        "saml_name_id": saml_name_id,
        "license_type": _text(source.get("license_type")),
        "is_github_com_user": _boolean(source.get("github_com_user")),
        "is_enterprise_server_user": _boolean(
            source.get("enterprise_server_user")
        ),
        "is_visual_studio_subscription_user": _boolean(
            source.get("visual_studio_subscription_user")
        ),
        "member_roles": _string_list(source.get("github_com_member_roles")),
        "enterprise_roles": enterprise_roles,
        "pending_org_invites": _string_list(
            source.get("github_com_orgs_with_pending_invites")
        ),
        "two_factor_auth_enabled": _boolean(
            source.get("github_com_two_factor_auth")
        ),
        "enterprise_server_user_ids": server_user_ids,
        "enterprise_server_emails": server_emails,
        "visual_studio_license_status": _text(
            source.get("visual_studio_license_status")
        ),
        "visual_studio_subscription_email": visual_studio_email,
        "total_user_accounts": _integer(source.get("total_user_accounts")),
        "advanced_security_enabled": None,
        "source_system": "GITHUB_CONSUMED_LICENSES_API",
        "source_api_version": context.api_version,
        "collector_version": context.collector_version,
        "source_record_hash": source_hash,
        # Use the immutable run timestamp so a Cloud Run task retry generates
        # byte-for-byte identical canonical NDJSON.
        "loaded_at": _utc_timestamp(context.captured_at),
    }


def serialize_ndjson(records: list[dict[str, Any]]) -> bytes:
    lines = (_stable_json(record) for record in records)
    return ("\n".join(lines) + "\n").encode("utf-8")
