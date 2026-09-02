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
import urllib.parse
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
    labels: tuple[str, ...] = ()

    @property
    def uri(self) -> str:
        return f"https://github.com/{self.repository}/issues/{self.number}"


class IssueSourceError(Exception):
    """분류된 source 오류. provider 응답 원문을 포함하지 않습니다."""

    def __init__(self, category: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.retry_after = retry_after


class IssueSource(Protocol):
    def fetch_issue(self, repository: str, number: int) -> IssueRecord: ...


class IssueLister(Protocol):
    """polling이 사용하는 목록 조회 경계."""

    def list_issues(
        self, repository: str, since: str | None = None, per_page: int = 50, max_pages: int = 10
    ) -> list[IssueRecord]: ...


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
        self._repository_ids: dict[str, str] = {}

    def fetch_issue(self, repository: str, number: int) -> IssueRecord:
        if number <= 0:
            raise IssueSourceError("invalid_request", "Issue 번호는 1 이상이어야 합니다.")

        repository_id = self._repository_id(repository)
        issue = self._get(f"/repos/{repository}/issues/{number}")
        return self._to_record(repository, repository_id, issue, fallback_number=number)

    def list_issues(
        self, repository: str, since: str | None = None, per_page: int = 50, max_pages: int = 10
    ) -> list[IssueRecord]:
        """Issue를 `updated` 오름차순으로 조회합니다.

        `since`는 polling cursor입니다. Pull Request는 결과에서 제외합니다.

        닫힌 Issue도 포함합니다. label 제거나 Issue 종료를 poller가 관찰해 승인을
        회수할 수 있어야 하기 때문입니다. open만 조회하면 닫힌 Issue가 목록에서
        사라져 reconciliation이 불가능합니다.
        """

        repository_id = self._repository_id(repository)
        records: list[IssueRecord] = []
        for page in range(1, max(1, max_pages) + 1):
            query = [
                "state=all",
                "sort=updated",
                "direction=asc",
                f"per_page={max(1, min(per_page, 100))}",
                f"page={page}",
            ]
            if since:
                query.append(f"since={urllib.parse.quote(since)}")
            payload = self._get(f"/repos/{repository}/issues?{'&'.join(query)}")
            if not isinstance(payload, list):
                raise IssueSourceError("invalid_response", "Issue 목록 응답 형식이 예상과 다릅니다.")

            for issue in payload:
                record = self._to_record(repository, repository_id, issue)
                if not record.is_pull_request:
                    records.append(record)
            if len(payload) < per_page:
                break
        return records

    def _repository_id(self, repository: str) -> str:
        if repository not in self._repository_ids:
            self._repository_ids[repository] = str(self._get(f"/repos/{repository}").get("id", ""))
        return self._repository_ids[repository]

    @staticmethod
    def _to_record(
        repository: str, repository_id: str, issue: dict, fallback_number: int = 0
    ) -> IssueRecord:
        user = issue.get("user") or {}
        labels = tuple(
            str(label.get("name", "")) if isinstance(label, dict) else str(label)
            for label in (issue.get("labels") or [])
        )
        return IssueRecord(
            repository=repository,
            repository_id=repository_id,
            number=int(issue.get("number", fallback_number)),
            issue_id=str(issue.get("id", "")),
            title=issue.get("title") or "",
            body=issue.get("body") or "",
            state=issue.get("state") or "",
            author=f"github:{user.get('login', 'unknown')}",
            created_at=issue.get("created_at") or "",
            updated_at=issue.get("updated_at") or "",
            is_pull_request="pull_request" in issue,
            labels=labels,
        )

    def _get(self, path: str):
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

        headers = headers or {}
        remaining = headers.get("X-RateLimit-Remaining")
        retry_after = _parse_retry_after(headers.get("Retry-After"))

        # 429는 header가 없어도 항상 rate limit입니다. 403은 quota 소진
        # (remaining == "0")이거나 secondary rate limit(Retry-After 제공)일 때
        # rate limit으로 분류합니다. secondary rate limit 응답은
        # X-RateLimit-Remaining을 포함하지 않으므로 remaining만 보면 놓칩니다.
        if status == 429 or (status == 403 and (remaining == "0" or retry_after is not None)):
            return IssueSourceError(
                "rate_limited", "GitHub API rate limit에 도달했습니다.", retry_after=retry_after
            )
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


def _parse_retry_after(value: str | None) -> int | None:
    """`Retry-After`의 초 단위 표기만 해석합니다. HTTP-date 형식은 무시합니다."""

    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return None
