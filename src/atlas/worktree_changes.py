"""executor 실행 전후의 worktree 상태를 비교합니다.

"process가 exit 0으로 끝났다"와 "Task가 구현됐다"는 다릅니다. 이 모듈은
후자를 판단할 근거를 모읍니다. 판정 자체는 provider와 무관하므로 adapter
밖에 둡니다.

이번 slice에는 validation pipeline이 없으므로 **탐지와 보고까지만** 합니다.
자동 rollback은 하지 않습니다. 되돌리는 판단은 사람이나 이후 slice의 몫입니다.
"""

from __future__ import annotations

import hashlib
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


@dataclass(frozen=True)
class WorktreeFingerprint:
    """worktree 내용의 지문.

    **파일 이름 목록만으로는 부족합니다.** 같은 파일의 내용만 바뀌면 이름
    집합은 그대로라 변경을 놓칩니다. 그래서 tracked 변경은 patch 바이트로,
    untracked 파일은 내용 해시로 요약합니다.

    raw source를 저장하지 않습니다. 남는 것은 digest와 개수뿐입니다.
    """

    head: str
    branch: str
    digest: str
    tracked_bytes: int = 0
    untracked_files: int = 0
    computed: bool = True
    reason: str = ""

    def matches(self, other: "WorktreeFingerprint | None") -> bool:
        if other is None or not (self.computed and other.computed):
            return False
        return (
            self.head == other.head
            and self.branch == other.branch
            and self.digest == other.digest
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "branch": self.branch,
            "digest": self.digest,
            "tracked_bytes": self.tracked_bytes,
            "untracked_files": self.untracked_files,
            "computed": self.computed,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "WorktreeFingerprint | None":
        if not isinstance(payload, dict) or not payload.get("digest"):
            return None
        return cls(
            head=str(payload.get("head") or ""),
            branch=str(payload.get("branch") or ""),
            digest=str(payload["digest"]),
            tracked_bytes=int(payload.get("tracked_bytes") or 0),
            untracked_files=int(payload.get("untracked_files") or 0),
            computed=bool(payload.get("computed", True)),
            reason=str(payload.get("reason") or ""),
        )


def fingerprint(
    worktree: Path | str, timeout_seconds: float = 30.0
) -> WorktreeFingerprint:
    """worktree 내용을 SHA-256 지문으로 요약합니다.

    포함하는 것입니다.

    - HEAD revision과 branch
    - `git diff HEAD --binary` 결과. tracked 파일의 추가·수정·삭제·mode 변경이
      모두 들어갑니다.
    - untracked 파일의 경로와 내용 해시

    포함하지 않는 것은 raw source입니다. 지문만 남깁니다.
    """

    git = GitRunner(worktree, timeout_seconds=timeout_seconds)
    digest = hashlib.sha256()
    try:
        head = git.head_revision()
        branch = git.current_branch()
    except (GitError, OSError) as error:
        return WorktreeFingerprint(
            head="", branch="", digest="", computed=False,
            reason=f"git_unreadable:{type(error).__name__}",
        )

    digest.update(head.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(branch.encode("utf-8"))
    digest.update(b"\x00")

    tracked_bytes = 0
    try:
        # patch 표현을 그대로 씁니다. 같은 파일의 내용 변경도 여기서 드러납니다.
        patch = git.run(
            "diff", "HEAD", "--binary", "--no-color", "--no-ext-diff"
        ).stdout
        raw = patch.encode("utf-8", errors="replace") if isinstance(patch, str) else patch
        tracked_bytes = len(raw)
        digest.update(raw)
    except (GitError, OSError) as error:
        return WorktreeFingerprint(
            head=head, branch=branch, digest="", computed=False,
            reason=f"diff_unreadable:{type(error).__name__}",
        )

    untracked = 0
    try:
        listed = git.run(
            "ls-files", "--others", "--exclude-standard"
        ).lines()
    except (GitError, OSError) as error:
        return WorktreeFingerprint(
            head=head, branch=branch, digest="", computed=False,
            reason=f"untracked_unreadable:{type(error).__name__}",
        )

    base = Path(worktree)
    for relative in sorted(listed):
        cleaned = relative.strip().strip('"')
        if not cleaned:
            continue
        untracked += 1
        digest.update(b"\x01")
        digest.update(cleaned.encode("utf-8"))
        digest.update(b"\x00")
        try:
            with open(base / cleaned, "rb") as handle:
                for chunk in iter(lambda: handle.read(65_536), b""):
                    digest.update(chunk)
        except OSError:
            # 읽지 못한 파일도 사실입니다. 무시하지 않고 지문에 반영합니다.
            digest.update(b"<unreadable>")

    return WorktreeFingerprint(
        head=head,
        branch=branch,
        digest=digest.hexdigest(),
        tracked_bytes=tracked_bytes,
        untracked_files=untracked,
    )


@dataclass(frozen=True)
class ContentDigest:
    """base revision 대비 변경 **내용**의 지문.

    `WorktreeFingerprint`와 목적이 다릅니다. 저쪽은 "지금 worktree가 그때와
    같은가"를 보고, 이쪽은 **commit 전후로 같은 값이 나오도록** 설계했습니다.

    commit하면 dirty 변경이 tree로 옮겨가므로 `git diff HEAD` 기반 지문은
    비어 버립니다. 그래서 base revision을 기준으로 잡고, 각 경로의 **blob
    해시**를 씁니다. `git hash-object`가 내는 값과 commit 안의 blob sha는
    같으므로 commit 전후 비교가 성립합니다.

    canonical 표현입니다.

    - 정렬된 `<status>\x00<path>\x00<blob-sha>` 목록
    - 수정·추가는 `M`, 삭제는 `D`
    - rename은 `--no-renames`로 삭제 + 추가로 펼칩니다. 표현이 안정적입니다
    - untracked 파일도 포함합니다

    담지 않는 것입니다.

    - raw source. digest와 개수만 남깁니다
    - 파일 mode. Windows에서 실행 비트를 신뢰할 수 없어 제외했습니다
    """

    digest: str
    entry_count: int = 0
    base: str = ""
    computed: bool = True
    reason: str = ""

    def matches(self, other: "ContentDigest | None") -> bool:
        if other is None or not (self.computed and other.computed):
            return False
        return bool(self.digest) and self.digest == other.digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "entry_count": self.entry_count,
            "base": self.base,
            "computed": self.computed,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "ContentDigest | None":
        if not isinstance(payload, dict) or not payload.get("digest"):
            return None
        return cls(
            digest=str(payload["digest"]),
            entry_count=int(payload.get("entry_count") or 0),
            base=str(payload.get("base") or ""),
            computed=bool(payload.get("computed", True)),
            reason=str(payload.get("reason") or ""),
        )


def _digest_entries(
    git: GitRunner, base: str, commit_ref: str | None
) -> list[tuple[str, str, str]]:
    """`(status, path, blob-sha)` 목록을 만듭니다.

    `commit_ref`가 있으면 그 commit을, 없으면 working tree를 봅니다. 두 경로가
    같은 내용에 대해 같은 값을 내야 합니다.
    """

    entries: list[tuple[str, str, str]] = []

    if commit_ref:
        raw = git.run("diff", "--name-status", "--no-renames", base, commit_ref).lines()
        for line in raw:
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status, path = parts[0].strip(), parts[-1].strip()
            if status.startswith("D"):
                entries.append(("D", path, ""))
            else:
                entries.append(("M", path, git.run("rev-parse", f"{commit_ref}:{path}").text))
        return sorted(set(entries))

    # working tree를 봅니다. tracked 변경과 untracked 파일을 모두 모읍니다.
    for line in git.run("diff", "--name-status", "--no-renames", base).lines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0].strip(), parts[-1].strip()
        if status.startswith("D"):
            entries.append(("D", path, ""))
        else:
            entries.append(("M", path, git.run("hash-object", "--", path).text))

    for line in git.run("ls-files", "--others", "--exclude-standard").lines():
        path = line.strip().strip('"')
        if path:
            entries.append(("M", path, git.run("hash-object", "--", path).text))

    return sorted(set(entries))


def content_digest(
    worktree: Path | str,
    base: str,
    commit_ref: str | None = None,
    timeout_seconds: float = 30.0,
) -> ContentDigest:
    """base 대비 변경 내용의 지문을 계산합니다.

    `commit_ref`를 주면 그 commit의 내용을, 주지 않으면 working tree의 내용을
    봅니다. 같은 내용이면 두 경우가 같은 digest를 냅니다.
    """

    git = GitRunner(worktree, timeout_seconds=timeout_seconds)
    if not base:
        return ContentDigest("", computed=False, reason="missing_base")
    try:
        entries = _digest_entries(git, base, commit_ref)
    except (GitError, OSError) as error:
        return ContentDigest(
            "", computed=False, base=base, reason=f"git_unreadable:{type(error).__name__}"
        )

    digest = hashlib.sha256()
    for status, path, sha in entries:
        digest.update(status.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(sha.encode("utf-8"))
        digest.update(b"\x01")
    return ContentDigest(
        digest=digest.hexdigest(), entry_count=len(entries), base=base
    )


def safe_content_digest(
    worktree: Path | str,
    base: str,
    commit_ref: str | None = None,
    timeout_seconds: float = 30.0,
) -> ContentDigest:
    """계산하지 못해도 예외를 던지지 않습니다. 실패는 `computed=False`입니다."""

    try:
        return content_digest(worktree, base, commit_ref, timeout_seconds)
    except (GitError, OSError) as error:
        return ContentDigest(
            "", computed=False, base=base, reason=f"unexpected:{type(error).__name__}"
        )


def safe_fingerprint(
    worktree: Path | str, timeout_seconds: float = 30.0
) -> WorktreeFingerprint:
    """지문을 계산하지 못해도 실행을 중단시키지 않습니다."""

    try:
        return fingerprint(worktree, timeout_seconds=timeout_seconds)
    except (GitError, OSError) as error:
        return WorktreeFingerprint(
            head="", branch="", digest="", computed=False,
            reason=f"unexpected:{type(error).__name__}",
        )


def safe_capture(worktree: Path | str, timeout_seconds: float = 30.0) -> WorktreeState | None:
    """git 상태를 읽지 못해도 실행 자체를 실패시키지 않습니다."""

    try:
        return capture_state(worktree, timeout_seconds=timeout_seconds)
    except (GitError, OSError):
        return None
