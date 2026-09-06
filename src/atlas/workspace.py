"""Run별 격리된 git branch와 worktree.

docs/adr/0010-task-execution-isolation.md와 docs/specs/execution-runtime.md의
Run Boundary를 구현합니다. executor process는 만들지 않습니다.

경계 규칙은 docs/security-governance.md에서 옵니다.

- worktree의 resolved path가 Project별 worker root 아래여야 합니다.
- path traversal과 symlink escape를 거부합니다.
- 사용자 입력을 branch 이름에 그대로 넣지 않습니다.
- `main`을 직접 checkout하거나 수정하지 않습니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .gitcmd import GitError, GitRunner

# Atlas가 만든 branch임을 나타내는 접두사. cleanup은 이 접두사와 DB provenance가
# 모두 일치할 때만 삭제를 허용합니다.
BRANCH_NAMESPACE = "atlas"

# branch 이름과 worktree 디렉터리 이름에 허용할 문자.
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")

# 어떤 경우에도 Atlas가 직접 쓰지 않는 branch.
PROTECTED_BRANCHES = frozenset({"main", "master", "HEAD", "trunk", "develop"})

_RUN_ID_SHORT_LENGTH = 12

# GitHub remote만 Task repository로 인정합니다. host를 정확히 확인하고
# owner/repo를 canonical 형태로 뽑아 exact equality로 비교합니다.
GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})

_SCP_LIKE = re.compile(r"^(?P<user>[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$")


def parse_github_remote(url: str) -> tuple[str, str] | None:
    """remote URL에서 `(host, "owner/repo")`를 뽑습니다.

    GitHub remote가 아니거나 형태가 모호하면 `None`을 돌려줍니다. suffix 비교는
    `https://github.com/evil/owner/repo.git` 같은 URL을 통과시키므로 쓰지 않고,
    경로 조각이 정확히 두 개인지 확인합니다.
    """

    raw = (url or "").strip()
    if not raw:
        return None

    host = ""
    path = ""

    if "://" in raw:
        scheme, _, rest = raw.partition("://")
        if scheme.lower() not in ("https", "http", "ssh", "git"):
            return None
        authority, _, path = rest.partition("/")
        # credential이 박힌 URL(https://user:token@host/...)에서 host만 취합니다.
        host = authority.rpartition("@")[2]
    else:
        match = _SCP_LIKE.match(raw)
        if match is None:
            return None
        host = match.group("host")
        path = match.group("path")

    # port를 떼어냅니다. IPv6 표기는 GitHub remote가 아니므로 다루지 않습니다.
    host = host.split(":", 1)[0].strip().lower()
    if not host:
        return None

    segments = [segment for segment in path.strip("/").split("/") if segment]
    if len(segments) != 2:
        # owner/repo가 아닌 경로는 모호하므로 거부합니다.
        return None

    owner, repo = segments
    repo = repo.removesuffix(".git")
    if not owner or not repo or owner.startswith(".") or repo.startswith("."):
        return None
    return host, f"{owner}/{repo}"




class WorkspaceError(Exception):
    """workspace 경계 또는 lifecycle 위반."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


class WorkspaceRecoveryRequired(WorkspaceError):
    """기록된 workspace가 실제 상태와 달라 사람 확인이 필요합니다.

    자동 복구나 재생성을 하지 않습니다. 어떤 invariant가 깨졌는지 boolean으로만
    들고 있어 event에 그대로 남겨도 경로가 노출되지 않습니다.
    """

    def __init__(self, message: str, checks: dict[str, bool], branch: str | None = None) -> None:
        super().__init__("workspace_recovery_required", message)
        self.checks = checks
        self.branch = branch

    def evidence(self) -> dict[str, Any]:
        """event log에 넣어도 되는 형태. 절대 경로를 담지 않습니다."""

        return {
            "category": self.category,
            "branch": self.branch,
            "checks": self.checks,
            "failed_checks": sorted(name for name, ok in self.checks.items() if not ok),
        }


@dataclass(frozen=True)
class WorkspacePlan:
    """git을 건드리기 전에 확정하는 workspace 계획."""

    run_id: str
    task_id: str
    branch: str
    worktree_path: str
    base_branch: str
    base_revision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "base_branch": self.base_branch,
            "base_revision": self.base_revision,
        }


def sanitize_segment(value: str) -> str:
    """사용자 입력에서 온 값을 ref/경로에 쓸 수 있는 조각으로 바꿉니다."""

    cleaned = _SAFE_SEGMENT.sub("-", (value or "").strip()).strip("-.")
    # git은 `..`와 선행/후행 점을 거부합니다.
    cleaned = cleaned.replace("..", "-")
    return cleaned or "unknown"


def branch_name(task_id: str, run_id: str) -> str:
    """결정적이고 충돌하지 않는 branch 이름.

    형식은 `atlas/<task-id>/<run-id-short>`입니다. `run_id`가 Run마다 고유하므로
    같은 Task의 retry Run도 서로 다른 branch를 씁니다.
    """

    task_segment = sanitize_segment(task_id)
    run_segment = sanitize_segment(run_id)[-_RUN_ID_SHORT_LENGTH:].strip("-.") or "run"
    return f"{BRANCH_NAMESPACE}/{task_segment}/{run_segment}"


def worktree_dirname(task_id: str, run_id: str) -> str:
    return f"{sanitize_segment(task_id)}-{sanitize_segment(run_id)[-_RUN_ID_SHORT_LENGTH:]}"


def is_atlas_branch(branch: str) -> bool:
    return branch.startswith(f"{BRANCH_NAMESPACE}/")


def resolve_within(root: Path, candidate: Path) -> Path:
    """`candidate`의 실제 경로가 `root` 아래인지 확인합니다.

    `resolve()`가 symlink를 따라가므로 symlink로 경계를 벗어나는 경로도 걸립니다.
    """

    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if resolved == resolved_root:
        raise WorkspaceError(
            "path_outside_root", f"worktree 경로가 worker root 자체입니다: {resolved}"
        )
    if not resolved.is_relative_to(resolved_root):
        raise WorkspaceError(
            "path_outside_root",
            f"worktree 경로가 허용된 worker root 밖입니다: {resolved}",
        )
    return resolved


class WorkspacePlanner:
    """repository 경계를 검증하고 workspace 계획을 만듭니다."""

    def __init__(
        self,
        repository_root: Path | str,
        workspaces_root: Path | str,
        *,
        repository: str | None = None,
        base_branch: str = "main",
        git_timeout_seconds: float = 30.0,
    ) -> None:
        self._repository_root = Path(repository_root).expanduser()
        self._workspaces_root = Path(workspaces_root).expanduser()
        self._repository = repository
        self._base_branch = base_branch
        self._git = GitRunner(self._repository_root, timeout_seconds=git_timeout_seconds)

    @property
    def git(self) -> GitRunner:
        return self._git

    @property
    def repository_root(self) -> Path:
        return self._repository_root

    @property
    def workspaces_root(self) -> Path:
        return self._workspaces_root

    def verify_repository(self) -> Path:
        """repository root가 실제 git repository인지 확인합니다."""

        root = self._repository_root
        if not root.exists():
            raise WorkspaceError("repository_not_found", f"repository root가 없습니다: {root}")
        if not self._git.is_repository():
            raise WorkspaceError(
                "not_a_repository", f"git repository가 아닙니다: {root}"
            )
        try:
            toplevel = self._git.toplevel()
        except GitError as error:
            raise WorkspaceError("not_a_repository", str(error)) from None
        if toplevel.resolve() != root.resolve():
            raise WorkspaceError(
                "not_repository_root",
                f"지정한 경로가 repository root가 아닙니다. toplevel={toplevel}",
            )
        return toplevel

    def verify_remote(self) -> str | None:
        """origin remote가 Task repository와 정확히 일치하는지 확인합니다.

        network를 쓰지 않고 로컬에 설정된 URL만 봅니다. remote가 없으면 확인하지
        않고 `None`을 돌려줍니다. 로컬 전용 repository를 막지 않기 위해서입니다.

        비교는 suffix가 아니라 canonical `owner/repo` 정확 일치입니다. host도
        GitHub인지 확인합니다.
        """

        if self._repository is None:
            return None
        url = self._git.remote_url()
        if url is None:
            return None

        parsed = parse_github_remote(url)
        if parsed is None:
            raise WorkspaceError(
                "remote_not_github",
                "origin remote를 GitHub owner/repo로 해석하지 못했습니다. "
                f"기대: {self._repository}",
            )
        host, slug = parsed
        if host not in GITHUB_HOSTS:
            raise WorkspaceError(
                "remote_not_github",
                f"origin remote host가 GitHub가 아닙니다: {host}",
            )
        if slug.lower() != self._repository.lower():
            raise WorkspaceError(
                "repository_mismatch",
                f"origin remote가 Task repository와 다릅니다. "
                f"기대: {self._repository}, 실제: {slug}",
            )
        return url

    def plan(self, run_id: str, task_id: str, base_branch: str | None = None) -> WorkspacePlan:
        """git을 변경하기 전에 branch, 경로, base revision을 확정합니다."""

        self.verify_repository()
        self.verify_remote()

        branch = branch_name(task_id, run_id)
        if branch.rsplit("/", 1)[-1] in PROTECTED_BRANCHES or branch in PROTECTED_BRANCHES:
            raise WorkspaceError("protected_branch", f"보호된 branch 이름입니다: {branch}")
        if not is_atlas_branch(branch):
            raise WorkspaceError("branch_not_owned", f"Atlas namespace 밖입니다: {branch}")
        if not self._git.is_valid_branch_name(branch):
            raise WorkspaceError("invalid_branch_name", f"git ref로 유효하지 않습니다: {branch}")
        if self._git.branch_exists(branch):
            raise WorkspaceError("branch_exists", f"branch가 이미 있습니다: {branch}")

        base = base_branch or self._base_branch
        if base in PROTECTED_BRANCHES and base != self._base_branch:
            raise WorkspaceError("invalid_base_branch", f"허용되지 않은 base branch: {base}")
        try:
            base_revision = self._git.resolve_revision(base)
        except GitError:
            raise WorkspaceError(
                "base_revision_unresolved", f"base branch를 해석하지 못했습니다: {base}"
            ) from None

        self._workspaces_root.mkdir(parents=True, exist_ok=True)
        candidate = self._workspaces_root / worktree_dirname(task_id, run_id)
        # 아직 없는 경로는 resolve()가 symlink를 못 따라가므로 부모로 검증합니다.
        resolve_within(self._workspaces_root, candidate.parent / candidate.name)

        return WorkspacePlan(
            run_id=run_id,
            task_id=task_id,
            branch=branch,
            worktree_path=str(candidate.resolve()),
            base_branch=base,
            base_revision=base_revision,
        )

    # -- git side effect -------------------------------------------------

    def create(self, plan: WorkspacePlan) -> Path:
        """계획대로 branch와 worktree를 만듭니다."""

        path = Path(plan.worktree_path)
        resolve_within(self._workspaces_root, path.parent / path.name)
        if path.exists():
            raise WorkspaceError("worktree_path_exists", f"경로가 이미 있습니다: {path}")
        try:
            self._git.add_worktree(path, plan.branch, plan.base_revision)
        except GitError as error:
            raise WorkspaceError("worktree_create_failed", str(error)) from error
        return path

    def validate(self, plan: WorkspacePlan) -> dict[str, Any]:
        """생성 결과가 계획과 일치하는지 확인합니다.

        docs 요구대로 toplevel, current branch, HEAD, resolved path, repository
        identity를 모두 봅니다.
        """

        path = Path(plan.worktree_path)
        if not path.exists():
            raise WorkspaceError("worktree_missing", f"worktree 경로가 없습니다: {path}")

        runner = GitRunner(path, timeout_seconds=self._git.timeout_seconds)
        try:
            toplevel = runner.toplevel().resolve()
            branch = runner.current_branch()
            head = runner.head_revision()
            common = runner.common_dir().resolve()
        except GitError as error:
            raise WorkspaceError("worktree_unreadable", str(error)) from error

        expected_path = path.resolve()
        expected_common = self._git.common_dir().resolve()
        checks = {
            "toplevel_matches": toplevel == expected_path,
            "branch_matches": branch == plan.branch,
            "head_matches_base": head == plan.base_revision,
            "path_within_root": expected_path.is_relative_to(self._workspaces_root.resolve()),
            "repository_matches": common == expected_common,
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise WorkspaceError(
                "workspace_validation_failed",
                f"workspace 검증 실패: {', '.join(failed)}",
            )
        return {
            "toplevel": str(toplevel),
            "branch": branch,
            "head": head,
            "checks": checks,
        }


    def validate_existing(self, branch: str, worktree_path: str) -> dict[str, Any]:
        """이미 있는 workspace가 여전히 쓸 수 있는 상태인지 확인합니다.

        `validate()`와 달리 HEAD가 base revision과 같은지는 보지 않습니다. 이미
        작업이 진행돼 commit이 쌓였을 수 있고, 그것은 정상입니다. 대신 executor가
        이 경로를 cwd로 신뢰할 수 있는지에 필요한 invariant만 확인합니다.

        불일치를 발견하면 자동으로 복구하거나 다시 만들지 않고 거부합니다.
        """

        path = Path(worktree_path)
        checks: dict[str, bool] = {
            "path_exists": path.exists(),
            "path_within_root": False,
            "registered_worktree": False,
            "branch_matches": False,
            "toplevel_matches": False,
            "repository_matches": False,
            # executor가 보호 branch 위에서 실행되면 안 됩니다. 기록된 branch
            # 이름과 실제 checkout 양쪽을 봅니다.
            "branch_not_protected": branch not in PROTECTED_BRANCHES,
        }

        if checks["path_exists"]:
            try:
                resolved = path.resolve()
                checks["path_within_root"] = resolved.is_relative_to(
                    self._workspaces_root.resolve()
                )
            except OSError:
                resolved = path
            registered = {
                Path(entry["worktree"]).resolve(): entry
                for entry in self._git.worktrees()
                if "worktree" in entry
            }
            entry = registered.get(resolved)
            checks["registered_worktree"] = entry is not None

            runner = GitRunner(path, timeout_seconds=self._git.timeout_seconds)
            try:
                checks["toplevel_matches"] = runner.toplevel().resolve() == resolved
                current = runner.current_branch()
                checks["branch_matches"] = current == branch
                checks["branch_not_protected"] = (
                    checks["branch_not_protected"] and current not in PROTECTED_BRANCHES
                )
                checks["repository_matches"] = (
                    runner.common_dir().resolve() == self._git.common_dir().resolve()
                )
            except GitError:
                # 읽을 수 없으면 나머지 검사는 실패로 둡니다.
                pass

        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise WorkspaceRecoveryRequired(
                f"기록된 workspace가 실제 상태와 다릅니다: {', '.join(failed)}",
                checks=checks,
                branch=branch,
            )
        return {"branch": branch, "checks": checks}

    def inspect(self, branch: str, worktree_path: str) -> dict[str, Any]:
        """현재 디스크 상태를 확인합니다. 상태를 바꾸지 않습니다."""

        path = Path(worktree_path)
        report: dict[str, Any] = {
            "path_exists": path.exists(),
            "branch_exists": self._git.branch_exists(branch),
            "registered_worktree": False,
            "branch_matches": None,
            "path_within_root": False,
            "dirty": None,
        }
        try:
            report["path_within_root"] = path.resolve().is_relative_to(
                self._workspaces_root.resolve()
            )
        except OSError:
            report["path_within_root"] = False

        registered = {
            Path(entry["worktree"]).resolve(): entry
            for entry in self._git.worktrees()
            if "worktree" in entry
        }
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        entry = registered.get(resolved)
        report["registered_worktree"] = entry is not None
        if entry is not None:
            report["branch_matches"] = entry.get("branch") == f"refs/heads/{branch}"

        if report["path_exists"] and report["registered_worktree"]:
            runner = GitRunner(path, timeout_seconds=self._git.timeout_seconds)
            try:
                report["dirty"] = runner.is_dirty()
            except GitError:
                report["dirty"] = None
        return report

    def remove_worktree(self, worktree_path: str, *, allow_dirty: bool = False) -> None:
        """Atlas가 만든 worktree를 제거합니다.

        dirty worktree는 기본적으로 거부합니다. 작업 내용이 남아 있을 수 있습니다.
        """

        path = Path(worktree_path)
        resolve_within(self._workspaces_root, path)
        if not path.exists():
            self._git.prune_worktrees()
            return

        runner = GitRunner(path, timeout_seconds=self._git.timeout_seconds)
        try:
            if not allow_dirty and runner.is_dirty():
                raise WorkspaceError(
                    "worktree_dirty",
                    f"worktree에 저장되지 않은 변경이 있습니다: {path}",
                )
        except GitError as error:
            raise WorkspaceError("worktree_unreadable", str(error)) from error

        try:
            self._git.remove_worktree(path, force=allow_dirty)
        except GitError as error:
            raise WorkspaceError("worktree_remove_failed", str(error)) from error

    def delete_branch(self, branch: str, *, force: bool = False) -> None:
        """Atlas namespace의 branch만 삭제합니다."""

        if not is_atlas_branch(branch):
            raise WorkspaceError(
                "branch_not_owned",
                f"Atlas가 만들지 않은 branch는 삭제하지 않습니다: {branch}",
            )
        if not self._git.branch_exists(branch):
            return
        try:
            self._git.delete_branch(branch, force=force)
        except GitError as error:
            raise WorkspaceError("branch_delete_failed", str(error)) from error
