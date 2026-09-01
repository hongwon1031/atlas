"""Intake 단계에서 사용하는 정책 값.

정규 정의는 docs/specs/task-schema.md와 docs/specs/github-event-ingestion.md입니다.
정책은 domain logic과 분리해 이 모듈에만 둡니다. 운영 storage 결정(ADR-005)이
`Proposed`이므로 현재 allowlist는 코드 상수로만 유지합니다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepositoryPolicy:
    """허용된 repository 하나에 대한 Workspace와 Project 경계."""

    workspace_id: str
    project_ids: frozenset[str]
    default_base_branch: str


REPOSITORY_ALLOWLIST: dict[str, RepositoryPolicy] = {
    "hongwon1031/atlas": RepositoryPolicy(
        workspace_id="personal",
        project_ids=frozenset({"atlas"}),
        default_base_branch="main",
    ),
}

# .github/ISSUE_TEMPLATE/atlas-task.yml의 title prefix. Issue Form marker로 사용합니다.
ISSUE_TITLE_MARKER = "[Atlas Task]"

# docs/specs/task-schema.md의 Scope Model 어휘.
SCOPE_OPERATIONS = frozenset({"create", "update", "delete", "deploy", "merge"})
EXTERNAL_SYSTEMS = frozenset({"production", "staging"})

# docs/adr/0003-initial-execution-environment.md에서 Accepted된 primary adapter.
PRIMARY_ADAPTER = "claude_code_self_hosted"

# .github/ISSUE_TEMPLATE/atlas-task.yml의 Safety Confirmations 문구.
# 개수만 세면 임의의 체크박스로 대체할 수 있으므로 문구 자체를 대조합니다.
REQUIRED_SAFETY_CONFIRMATIONS: tuple[tuple[str, str], ...] = (
    ("secret_free", "이 Task에는 secret, 개인정보, 회사 내부 정보가 포함되지 않았습니다."),
    (
        "no_direct_main_write",
        "AI가 `main`에 직접 push하거나 merge해서는 안 된다는 점을 확인했습니다.",
    ),
    (
        "reviewable_criteria",
        "완료 조건과 허용 범위를 사람이 검토할 수 있을 만큼 구체적으로 작성했습니다.",
    ),
)


def normalize_confirmation(text: str) -> str:
    """공백과 backtick 표기 차이를 무시하고 confirmation 문구를 비교합니다."""

    return " ".join(text.replace("`", "").split()).casefold()


def repository_policy(repository: str) -> RepositoryPolicy | None:
    return REPOSITORY_ALLOWLIST.get(repository)
