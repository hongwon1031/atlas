"""GitHub Pull Request 생성 경계.

provider-neutral하게 씁니다. Atlas core는 "draft PR을 만들고 이미 있으면
찾아온다"는 계약만 알고, GitHub REST 세부사항은 이 파일 안에 둡니다.

runtime dependency를 추가하지 않습니다. 표준 라이브러리 `urllib`만 씁니다.

## credential

**Atlas는 token 값을 읽어 저장하지 않습니다.**

- argv에 넣지 않습니다.
- DB에 넣지 않습니다.
- log와 event에 넣지 않습니다.
- 오류 메시지에 넣지 않습니다.

환경변수에서 읽어 Authorization 헤더로만 씁니다. 값 자체는 이 객체 밖으로
나가지 않고, 출력에는 존재 여부만 남습니다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from .publication_models import PublicationFailure, PublicationError, PullRequestRef

API_ROOT = "https://api.github.com"
USER_AGENT = "atlas-worker"
DEFAULT_TIMEOUT_SECONDS = 30.0

# issue_source와 같은 순서로 찾습니다. Atlas 전용 token을 먼저 봅니다.
TOKEN_ENV_VARS = ("ATLAS_GITHUB_TOKEN", "GITHUB_TOKEN")

# PR 본문 길이 상한. 검증 로그 전문을 붙이지 않습니다.
MAX_BODY_CHARS = 8_000


class PullRequestClient(Protocol):
    """PR을 만들고 찾는 경계."""

    def find_open(
        self, repository: str, head_branch: str, base_branch: str
    ) -> list[PullRequestRef]:
        """같은 head/base로 열려 있는 PR을 찾습니다."""
        ...

    def create_draft(
        self, repository: str, head_branch: str, base_branch: str, title: str, body: str
    ) -> PullRequestRef:
        """draft PR을 만듭니다."""
        ...


def resolve_token(environ: dict[str, str] | None = None) -> str | None:
    """환경에서 token을 찾습니다. 값은 돌려주되 저장하지 않습니다."""

    import os

    env = environ if environ is not None else dict(os.environ)
    for name in TOKEN_ENV_VARS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def _pull_request_from_payload(payload: dict[str, Any]) -> PullRequestRef:
    head = ((payload.get("head") or {}).get("ref")) or ""
    base = ((payload.get("base") or {}).get("ref")) or ""
    return PullRequestRef(
        number=int(payload.get("number") or 0),
        url=str(payload.get("html_url") or ""),
        state=str(payload.get("state") or "open"),
        draft=bool(payload.get("draft")),
        node_id=str(payload.get("node_id") or ""),
        head=head,
        base=base,
    )


class GitHubPullRequestClient:
    """GitHub REST API로 draft PR을 다룹니다."""

    def __init__(
        self,
        token: str | None = None,
        api_root: str = API_ROOT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._token = token if token is not None else resolve_token()
        self._api_root = api_root.rstrip("/")
        self._timeout = timeout_seconds

    @property
    def authenticated(self) -> bool:
        """token 값이 아니라 존재 여부만 노출합니다."""

        return bool(self._token)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self._api_root}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=data, headers=self._headers(), method=method
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise self._classify(error.code, detail) from None
        except urllib.error.URLError as error:
            raise PublicationError(
                PublicationFailure.PR_CREATE_FAILED,
                f"GitHub에 연결하지 못했습니다: {type(error).__name__}",
            ) from None
        try:
            return json.loads(body) if body else None
        except ValueError:
            raise PublicationError(
                PublicationFailure.PR_CREATE_FAILED, "GitHub 응답을 해석하지 못했습니다."
            ) from None

    @staticmethod
    def _classify(status: int, detail: str) -> PublicationError:
        """HTTP 오류를 publication 어휘로 옮깁니다.

        detail에는 token이 들어가지 않습니다. 요청 헤더는 응답 본문에
        되돌아오지 않습니다.
        """

        if status in (401, 403):
            return PublicationError(
                PublicationFailure.AUTHENTICATION_FAILED,
                f"GitHub 인증에 실패했습니다(HTTP {status}).",
                {"status": status},
            )
        if status == 422:
            # 같은 head/base로 PR이 이미 있거나 branch가 없습니다.
            return PublicationError(
                PublicationFailure.PR_CONFLICT,
                "GitHub가 PR 생성을 거부했습니다. 이미 있거나 조건이 맞지 않습니다.",
                {"status": status, "detail": detail[:200]},
            )
        return PublicationError(
            PublicationFailure.PR_CREATE_FAILED,
            f"GitHub 요청이 실패했습니다(HTTP {status}).",
            {"status": status, "detail": detail[:200]},
        )

    # -- PullRequestClient ------------------------------------------------

    def find_open(
        self, repository: str, head_branch: str, base_branch: str
    ) -> list[PullRequestRef]:
        owner, _, name = repository.partition("/")
        # head는 `owner:branch` 형식이어야 합니다.
        query = urllib.parse.urlencode(
            {
                "state": "open",
                "head": f"{owner}:{head_branch}",
                "base": base_branch,
                "per_page": "10",
            }
        )
        payload = self._request("GET", f"/repos/{owner}/{name}/pulls?{query}")
        if not isinstance(payload, list):
            return []
        return [_pull_request_from_payload(item) for item in payload if isinstance(item, dict)]

    def create_draft(
        self, repository: str, head_branch: str, base_branch: str, title: str, body: str
    ) -> PullRequestRef:
        owner, _, name = repository.partition("/")
        payload = self._request(
            "POST",
            f"/repos/{owner}/{name}/pulls",
            {
                "title": title,
                "head": head_branch,
                "base": base_branch,
                "body": body,
                # 항상 draft입니다. Atlas는 ready-for-review로 바꾸지 않습니다.
                "draft": True,
            },
        )
        if not isinstance(payload, dict):
            raise PublicationError(
                PublicationFailure.PR_CREATE_FAILED, "GitHub 응답이 비어 있습니다."
            )
        return _pull_request_from_payload(payload)
