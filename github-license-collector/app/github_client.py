from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import jwt
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.config import Settings


class GitHubApiError(RuntimeError):
    """Raised for invalid or unsuccessful GitHub API responses."""


@dataclass(frozen=True)
class FetchedLicenses:
    users: list[dict[str, Any]]
    raw_pages: list[dict[str, Any]]
    total_seats_consumed: int | None
    total_seats_purchased: int | None
    pages_fetched: int
    pagination_complete: bool
    request_ids: list[str]
    rate_limit_remaining: int | None


class GitHubLicenseClient:
    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
    ) -> None:
        self._settings = settings
        self._session = session or self._new_session()

    @staticmethod
    def _new_session() -> requests.Session:
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        return session

    @property
    def _timeout(self) -> tuple[int, int]:
        return (
            self._settings.http_connect_timeout_seconds,
            self._settings.http_read_timeout_seconds,
        )

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 540,
            "iss": self._settings.github_app_id,
        }
        try:
            return jwt.encode(
                payload,
                self._settings.github_app_private_key,
                algorithm="RS256",
            )
        except Exception as exc:  # Secret contents must never enter the message.
            raise GitHubApiError(
                "Unable to create GitHub App JWT; verify the App ID and PEM key"
            ) from exc

    def create_installation_token(self) -> str:
        url = (
            f"{self._settings.github_api_url}/app/installations/"
            f"{self._settings.github_app_installation_id}/access_tokens"
        )
        response = self._session.post(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._app_jwt()}",
                "X-GitHub-Api-Version": self._settings.github_api_version,
                "User-Agent": "github-license-collector",
            },
            timeout=self._timeout,
        )
        if response.status_code != 201:
            raise GitHubApiError(
                "GitHub installation-token request failed with HTTP "
                f"{response.status_code}: {self._safe_message(response)}"
            )
        body = response.json()
        token = body.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubApiError(
                "GitHub installation-token response did not contain a token"
            )
        return token

    def fetch_consumed_licenses(self, installation_token: str) -> FetchedLicenses:
        enterprise = quote(self._settings.github_enterprise_slug, safe="")
        next_url: str | None = (
            f"{self._settings.github_api_url}/enterprises/{enterprise}/"
            "consumed-licenses"
        )
        params: dict[str, int] | None = {"per_page": 100, "page": 1}
        users: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        request_ids: list[str] = []
        total_consumed: int | None = None
        total_purchased: int | None = None
        rate_limit_remaining: int | None = None
        configured_origin = urlsplit(self._settings.github_api_url)

        while next_url:
            if len(pages) >= self._settings.max_github_pages:
                raise GitHubApiError(
                    "GitHub pagination exceeded MAX_GITHUB_PAGES; aborting"
                )

            next_origin = urlsplit(next_url)
            if (
                next_origin.scheme != "https"
                or next_origin.netloc != configured_origin.netloc
            ):
                raise GitHubApiError(
                    "GitHub pagination returned a URL outside the configured "
                    "API origin; refusing to forward the installation token"
                )

            response = self._session.get(
                next_url,
                params=params,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {installation_token}",
                    "X-GitHub-Api-Version": self._settings.github_api_version,
                    "User-Agent": "github-license-collector",
                },
                timeout=self._timeout,
            )
            params = None
            if response.status_code != 200:
                raise GitHubApiError(
                    "GitHub consumed-licenses request failed with HTTP "
                    f"{response.status_code}: {self._safe_message(response)}"
                )

            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("users"), list):
                raise GitHubApiError(
                    "GitHub consumed-licenses response has an unexpected schema"
                )

            if total_consumed is None:
                total_consumed = self._optional_int(
                    body.get("total_seats_consumed")
                )
                total_purchased = self._optional_int(
                    body.get("total_seats_purchased")
                )

            pages.append(body)
            users.extend(
                user for user in body["users"] if isinstance(user, dict)
            )

            request_id = response.headers.get("X-GitHub-Request-Id")
            if request_id:
                request_ids.append(request_id)
            rate_limit_remaining = self._optional_int(
                response.headers.get("X-RateLimit-Remaining")
            )

            next_url = response.links.get("next", {}).get("url")

        return FetchedLicenses(
            users=users,
            raw_pages=pages,
            total_seats_consumed=total_consumed,
            total_seats_purchased=total_purchased,
            pages_fetched=len(pages),
            pagination_complete=True,
            request_ids=request_ids,
            rate_limit_remaining=rate_limit_remaining,
        )

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_message(response: requests.Response) -> str:
        try:
            body = response.json()
            message = str(body.get("message", "GitHub API error"))
        except (ValueError, AttributeError):
            message = "GitHub API error"
        return message[:500]
