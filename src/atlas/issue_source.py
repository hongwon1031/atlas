"""Issue source boundary와 GitHub REST adapter.

`IssueSource`는 provider-neutral 경계입니다. GitHub 세부사항은
`GitHubRestIssueSource` 안에만 둡니다(docs/architecture.md의 Adapter 원칙).

docs/specs/github-event-ingestion.md에 따라 token은 repository에 저장하지 않고
환경에서 주입하며, provider 오류 원문은 로그나 결과에 남기지 않습니다.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 10.0
TOKEN_ENV_VARS = ("ATLAS_GITHUB_TOKEN", "GITHUB_TOKEN")


@dataclass(frozen=True)
class IssueRecord:
    repository: str
    repository_id: str
    number: int
    issue_id: str
    title: str
    body: str
    state: str
    author: str
    created_at: str
    updated_at: str
    is_pull_request: bool = False

    @property
    def uri(self) -> str:
        return f"https://github.com/{self.repository}/issues/{self.number}"


class IssueSourceError(Exception):
    """분류된 source 오류. provider 응답 원문을 포함하지 않습니다."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


class IssueSource(Protocol):
    def fetch_issue(self, repository: str, number: int) -> IssueRecord: ...


def resolve_token(environ: dict[str, str] | None = None) -> str | None:
    env = environ if environ is not None else dict(os.environ)
    for name in TOKEN_ENV_VARS:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


class GitHubRestIssueSource:
    """표준 라이브러리만 사용하는 GitHub REST adapter."""

    def __init__(
        self,
        token: str | None = None,
        api_base: str = DEFAULT_API_BASE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._token = token if token is not None else resolve_token()
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def fetch_issue(self, repository: str, number: int) -> IssueRecord:
        if number <= 0:
            raise IssueSourceError("invalid_request", "Issue 번호는 1 이상이어야 합니다.")

        repo = self._get(f"/repos/{repository}")
        issue = self._get(f"/repos/{repository}/issues/{number}")
        user = issue.get("user") or {}

        return IssueRecord(
            repository=repository,
            repository_id=str(repo.get("id", "")),
            number=int(issue.get("number", number)),
            issue_id=str(issue.get("id", "")),
            title=issue.get("title") or "",
            body=issue.get("body") or "",
            state=issue.get("state") or "",
            author=f"github:{user.get('login', 'unknown')}",
            created_at=issue.get("created_at") or "",
            updated_at=issue.get("updated_at") or "",
            is_pull_request="pull_request" in issue,
        )

    def _get(self, path: str) -> dict:
        request = urllib.request.Request(
            f"{self._api_base}{path}",
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise self._classify(error.code, error.headers) from None
        except urllib.error.URLError:
            raise IssueSourceError("network", "GitHub에 연결하지 못했습니다.") from None
        except (ValueError, TypeError):
            raise IssueSourceError("invalid_response", "GitHub 응답을 해석하지 못했습니다.") from None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "atlas-issue-intake",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    @staticmethod
    def _classify(status: int, headers) -> IssueSourceError:
        """HTTP 상태를 분류합니다. 응답 body는 읽지도 기록하지도 않습니다."""

        remaining = (headers or {}).get("X-RateLimit-Remaining")
        if status in (403, 429) and remaining == "0":
            return IssueSourceError("rate_limited", "GitHub API rate limit에 도달했습니다.")
        if status in (401, 403):
            return IssueSourceError(
                "authentication",
                "GitHub 인증 또는 권한이 부족합니다. 토큰 범위를 확인하세요.",
            )
        if status == 404:
            return IssueSourceError(
                "not_found",
                "Issue 또는 repository를 찾을 수 없습니다. 접근 권한도 확인하세요.",
            )
        if status >= 500:
            return IssueSourceError("provider_unavailable", "GitHub가 일시적으로 응답하지 않습니다.")
        return IssueSourceError("provider_error", f"GitHub 요청이 실패했습니다. (HTTP {status})")
