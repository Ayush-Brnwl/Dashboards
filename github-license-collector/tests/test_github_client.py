from dataclasses import replace

from app.config import Settings
from app.github_client import GitHubLicenseClient


class FakeResponse:
    def __init__(self, body, *, links=None, headers=None, status_code=200):
        self._body = body
        self.links = links or {}
        self.headers = headers or {}
        self.status_code = status_code

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self._responses)


def settings() -> Settings:
    return Settings(
        project_id="example-project",
        bq_dataset_id="dataset",
        bq_snapshot_table_id="snapshot",
        bq_run_table_id="runs",
        bq_location="EU",
        gcs_bucket="bucket",
        gcs_prefix="prefix",
        github_api_url="https://api.github.com",
        github_enterprise_slug="enterprise with space",
        github_app_id="123",
        github_app_installation_id=456,
        github_app_private_key="not-used-by-this-test",
    )


def test_fetches_every_page_and_collects_totals() -> None:
    page_two_url = "https://api.github.com/next-page"
    session = FakeSession(
        [
            FakeResponse(
                {
                    "total_seats_consumed": 2,
                    "total_seats_purchased": 10,
                    "users": [{"github_com_login": "one"}],
                },
                links={"next": {"url": page_two_url}},
                headers={
                    "X-GitHub-Request-Id": "request-1",
                    "X-RateLimit-Remaining": "4999",
                },
            ),
            FakeResponse(
                {"users": [{"github_com_login": "two"}]},
                headers={
                    "X-GitHub-Request-Id": "request-2",
                    "X-RateLimit-Remaining": "4998",
                },
            ),
        ]
    )
    client = GitHubLicenseClient(settings(), session=session)

    result = client.fetch_consumed_licenses("installation-token")

    assert [item["github_com_login"] for item in result.users] == [
        "one",
        "two",
    ]
    assert result.pages_fetched == 2
    assert result.total_seats_consumed == 2
    assert result.total_seats_purchased == 10
    assert result.request_ids == ["request-1", "request-2"]
    assert result.rate_limit_remaining == 4998
    assert session.calls[0][1]["params"] == {"per_page": 100, "page": 1}
    assert session.calls[1][0] == page_two_url
    assert session.calls[1][1]["params"] is None


def test_page_limit_stops_unbounded_pagination() -> None:
    configured = replace(settings(), max_github_pages=1)
    session = FakeSession(
        [
            FakeResponse(
                {"users": []},
                links={"next": {"url": "https://api.github.com/page-2"}},
            )
        ]
    )
    client = GitHubLicenseClient(configured, session=session)

    try:
        client.fetch_consumed_licenses("installation-token")
    except RuntimeError as exc:
        assert "MAX_GITHUB_PAGES" in str(exc)
    else:
        raise AssertionError("Expected page-limit failure")


def test_refuses_to_forward_token_to_another_pagination_host() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {"users": []},
                links={"next": {"url": "https://attacker.example/page-2"}},
            )
        ]
    )
    client = GitHubLicenseClient(settings(), session=session)

    try:
        client.fetch_consumed_licenses("installation-token")
    except RuntimeError as exc:
        assert "outside the configured API origin" in str(exc)
    else:
        raise AssertionError("Expected cross-origin pagination failure")
