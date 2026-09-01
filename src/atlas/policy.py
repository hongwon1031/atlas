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


def repository_policy(repository: str) -> RepositoryPolicy | None:
    return REPOSITORY_ALLOWLIST.get(repository)
