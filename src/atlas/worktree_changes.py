"""executor 실행 전후의 worktree 상태를 비교합니다.

"process가 exit 0으로 끝났다"와 "Task가 구현됐다"는 다릅니다. 이 모듈은
후자를 판단할 근거를 모읍니다. 판정 자체는 provider와 무관하므로 adapter
밖에 둡니다.

이번 slice에는 validation pipeline이 없으므로 **탐지와 보고까지만** 합니다.
자동 rollback은 하지 않습니다. 되돌리는 판단은 사람이나 이후 slice의 몫입니다.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .gitcmd import GitError, GitRunner

# 보고에 담을 파일 경로 개수 상한. event와 log가 무한히 커지지 않게 합니다.
MAX_REPORTED_PATHS = 200


class ImplementationOutcome(str, Enum):
    """구현 결과. executor process의 성공/실패와 별개입니다."""

    # worktree가 실제로 바뀌었습니다.
    CHANGES_APPLIED = "changes_applied"
    # process는 정상 종료했지만 아무것도 바뀌지 않았습니다.
    NO_CHANGES = "no_changes"
    # 바뀌긴 했지만 허용 범위를 벗어났거나 금지된 변경이 섞였습니다.
    POLICY_VIOLATION = "policy_violation"
    # process가 실패해 구현 결과를 판단할 수 없습니다.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorktreeState:
    """한 시점의 worktree 상태."""

    head: str
    branch: str
    entries: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"head": self.head, "branch": self.branch, "entry_count": len(self.entries)}


@dataclass(frozen=True)
class ChangeReport:
    """실행 전후 비교 결과."""

    outcome: ImplementationOutcome
    changed_files: tuple[str, ...] = ()
    head_changed: bool = False
    committed: bool = False
    branch_changed: bool = False
    git_internal_paths: tuple[str, ...] = ()
    out_of_scope_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    before: WorktreeState | None = None
    after: WorktreeState | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_files) or self.head_changed

    @property
    def violations(self) -> tuple[str, ...]:
        """policy 위반으로 볼 근거들."""

        reasons: list[str] = []
        if self.committed:
            reasons.append("unexpected_commit")
        if self.branch_changed:
            reasons.append("branch_switched")
        if self.git_internal_paths:
            reasons.append("git_internals_modified")
        if self.forbidden_paths:
            reasons.append("forbidden_path_changed")
        if self.out_of_scope_paths:
            reasons.append("out_of_scope_path_changed")
        return tuple(reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "changed_file_count": len(self.changed_files),
            "changed_files": list(self.changed_files[:MAX_REPORTED_PATHS]),
            "head_changed": self.head_changed,
            "committed": self.committed,
            "branch_changed": self.branch_changed,
            "git_internal_paths": list(self.git_internal_paths[:MAX_REPORTED_PATHS]),
            "out_of_scope_paths": list(self.out_of_scope_paths[:MAX_REPORTED_PATHS]),
            "forbidden_paths": list(self.forbidden_paths[:MAX_REPORTED_PATHS]),
            "violations": list(self.violations),
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
        }


def capture_state(worktree: Path | str, timeout_seconds: float = 30.0) -> WorktreeState:
    """HEAD, branch, `git status --porcelain`을 한 시점 스냅샷으로 남깁니다."""

    git = GitRunner(worktree, timeout_seconds=timeout_seconds)
    entries = git.run("status", "--porcelain=v1", "--untracked-files=all").lines()
    return WorktreeState(
        head=git.head_revision(),
        branch=git.current_branch(),
        entries=tuple(sorted(entries)),
    )


def _paths_from_entries(entries: tuple[str, ...]) -> set[str]:
    """porcelain 줄에서 경로만 뽑습니다.

    rename은 `R  old -> new` 형태라 양쪽을 모두 변경으로 봅니다.
    """

    paths: set[str] = set()
    for entry in entries:
        if len(entry) < 4:
            continue
        body = entry[3:]
        for part in body.split(" -> "):
            cleaned = part.strip().strip('"')
            if cleaned:
                paths.add(cleaned.replace("\\", "/"))
    return paths


def _matches(path: str, pattern: str) -> bool:
    """glob 또는 디렉터리 접두사로 경로를 판정합니다.

    Issue Form의 scope는 자유 서술이라 `src/`, `src/**`, `README.md`가 모두
    올 수 있습니다. 셋 다 받아들입니다.
    """

    candidate = path.strip().strip("/")
    rule = pattern.strip().strip('"').replace("\\", "/").strip()
    if not rule:
        return False
    rule = rule.rstrip("/")
    if not rule:
        return False
    if posixpath.normpath(rule) == posixpath.normpath(candidate):
        return True
    if rule.endswith("/**"):
        rule = rule[:-3]
    if candidate == rule or candidate.startswith(rule + "/"):
        return True
    return _fnmatch(candidate, rule)


def _fnmatch(path: str, pattern: str) -> bool:
    from fnmatch import fnmatchcase

    if fnmatchcase(path, pattern):
        return True
    # `**`를 쓰는 표기를 단순 glob으로도 한 번 더 봅니다.
    return fnmatchcase(path, pattern.replace("**/", "*/").replace("**", "*"))


def _scope_patterns(scope: Any) -> tuple[str, ...]:
    if not isinstance(scope, dict):
        return ()
    values = scope.get("paths") or ()
    return tuple(str(v) for v in values if str(v or "").strip())


def compare(
    before: WorktreeState,
    after: WorktreeState,
    task: dict[str, Any] | None = None,
    process_succeeded: bool = True,
) -> ChangeReport:
    """전후 상태를 비교해 구현 결과를 판정합니다."""

    changed = sorted(_paths_from_entries(after.entries) | _paths_from_entries(before.entries))
    # 실행 전부터 있던 변경은 이번 실행의 결과가 아닙니다. 다만 없어진 것도
    # 변경이므로 양쪽 차집합을 모두 봅니다.
    before_set, after_set = set(before.entries), set(after.entries)
    if before_set == after_set:
        changed = []
    else:
        touched = _paths_from_entries(tuple(after_set ^ before_set))
        changed = sorted(touched)

    head_changed = before.head != after.head
    branch_changed = before.branch != after.branch

    git_internal = tuple(p for p in changed if p == ".git" or p.startswith(".git/"))

    allowed = _scope_patterns((task or {}).get("allowed_scope"))
    forbidden = _scope_patterns((task or {}).get("forbidden_scope"))
    forbidden_hits = tuple(p for p in changed if any(_matches(p, r) for r in forbidden))
    # allowed_scope가 비어 있으면 Task가 범위를 명시하지 않은 것입니다.
    # 그 경우 범위 밖이라고 단정하지 않습니다. schema가 약한 상태이므로
    # 없는 근거로 위반을 만들지 않습니다.
    out_of_scope = (
        tuple(p for p in changed if not any(_matches(p, r) for r in allowed))
        if allowed
        else ()
    )

    if not process_succeeded:
        outcome = ImplementationOutcome.UNKNOWN
    elif head_changed or branch_changed or git_internal or forbidden_hits or out_of_scope:
        outcome = ImplementationOutcome.POLICY_VIOLATION
    elif changed:
        outcome = ImplementationOutcome.CHANGES_APPLIED
    else:
        outcome = ImplementationOutcome.NO_CHANGES

    return ChangeReport(
        outcome=outcome,
        changed_files=tuple(changed),
        head_changed=head_changed,
        committed=head_changed,
        branch_changed=branch_changed,
        git_internal_paths=git_internal,
        out_of_scope_paths=out_of_scope,
        forbidden_paths=forbidden_hits,
        before=before,
        after=after,
    )


def safe_capture(worktree: Path | str, timeout_seconds: float = 30.0) -> WorktreeState | None:
    """git 상태를 읽지 못해도 실행 자체를 실패시키지 않습니다."""

    try:
        return capture_state(worktree, timeout_seconds=timeout_seconds)
    except (GitError, OSError):
        return None
