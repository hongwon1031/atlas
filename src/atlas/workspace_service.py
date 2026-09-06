"""Workspace lifecycle orchestration.

DB 기록과 git side effect를 순서대로 엮습니다. git 작업을 DB transaction 안에서
잡지 않으려고 다음 단계로 나눕니다.

    begin_workspace(preparing)  ->  git branch/worktree  ->  validate
                                                          ->  attach(ready)
                                    실패 시 fail_workspace(failed)

`preparing` 기록이 git보다 먼저 남으므로 중간에 죽어도 어떤 branch와 경로를
정리해야 하는지 DB만 보고 알 수 있습니다. 이것이 orphan 식별의 근거입니다.

cleanup 정책은 docs/specs/execution-runtime.md의 Cleanup Matrix를 따릅니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .gitcmd import GitError
from .schema import Run, RunStatus, WorkspaceStatus
from .store import RunError, TaskStore, WorkspaceConflict
from .workspace import (
    WorkspaceError,
    WorkspacePlanner,
    WorkspaceRecoveryRequired,
    is_atlas_branch,
)

# Cleanup Matrix: 어떤 종료 상태에서 branch를 남길지.
# 작업 내용이 남아 있을 수 있으므로 기본은 보수적으로 보존입니다.
BRANCH_RETENTION: dict[RunStatus, bool] = {
    RunStatus.SUCCEEDED: True,   # PR lifecycle 동안 유지
    RunStatus.FAILED: True,      # retry 판단까지 보존
    RunStatus.CANCELLED: True,   # push되지 않은 상태를 보존
    RunStatus.ORPHANED: True,    # 사람 확인 전까지 보존
}


@dataclass(frozen=True)
class WorkspaceResult:
    run: Run
    created: bool
    validation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "run": self.run.to_dict(),
            "validation": self.validation,
        }


@dataclass(frozen=True)
class CleanupResult:
    run_id: str
    worktree_removed: bool
    branch_kept: bool
    reason: str
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "worktree_removed": self.worktree_removed,
            "branch_kept": self.branch_kept,
            "reason": self.reason,
            "error": self.error,
        }


class WorkspaceService:
    def __init__(self, store: TaskStore, planner: WorkspacePlanner) -> None:
        self._store = store
        self._planner = planner

    @property
    def planner(self) -> WorkspacePlanner:
        return self._planner

    def create(self, run_id: str) -> WorkspaceResult:
        """Run에 격리된 branch와 worktree를 준비합니다.

        같은 Run에 두 번 호출하면 새로 만들지 않고 `created=False`로 기존
        workspace를 돌려줍니다.
        """

        run = self._store.run(run_id)
        if run is None:
            raise RunError("run_not_found", f"{run_id}를 찾을 수 없습니다.")

        if run.workspace_status is WorkspaceStatus.READY:
            # DB 기록만 믿지 않습니다. executor가 이 경로를 cwd로 신뢰할
            # 예정이므로 stale하거나 손상된 workspace를 정상으로 돌려주면
            # 안 됩니다. 실제 git 상태를 다시 확인합니다.
            return WorkspaceResult(
                run=run, created=False, validation=self._revalidate(run)
            )

        plan = self._planner.plan(run_id, run.task_id, base_branch=run.base_branch)

        try:
            self._store.begin_workspace(
                run_id,
                branch=plan.branch,
                worktree_path=plan.worktree_path,
                base_branch=plan.base_branch,
                base_revision=plan.base_revision,
            )
        except WorkspaceConflict as conflict:
            # 경쟁적으로 다른 호출이 먼저 READY로 만들었을 수 있습니다. 이
            # 경로에서도 DB 기록만 믿지 않고 실제 상태를 재검증합니다.
            if conflict.run is not None and conflict.run.workspace_status is WorkspaceStatus.READY:
                return WorkspaceResult(
                    run=conflict.run,
                    created=False,
                    validation=self._revalidate(conflict.run),
                )
            raise

        try:
            self._planner.create(plan)
            validation = self._planner.validate(plan)
        except (WorkspaceError, GitError) as error:
            # 실패 근거를 redaction해서 남깁니다. preparing 기록이 유지되므로
            # 남은 branch/worktree를 나중에 식별해 정리할 수 있습니다.
            detail = (
                error.redacted()
                if isinstance(error, GitError)
                else {"category": error.category, "detail": error.message}
            )
            self._store.fail_workspace(run_id, detail)
            raise WorkspaceError(
                detail.get("category", "workspace_create_failed"),
                "workspace 준비에 실패했습니다. 남은 리소스는 cleanup 대상으로 기록했습니다.",
            ) from None

        attached = self._store.attach_workspace(run_id, evidence={"head": validation["head"]})
        return WorkspaceResult(run=attached, created=True, validation=validation)

    def _revalidate(self, run: Run) -> dict[str, Any]:
        """READY workspace를 재사용하기 전에 실제 상태를 확인합니다.

        불일치는 자동 복구하지 않고 근거를 남긴 뒤 거부합니다.
        """

        if not run.branch or not run.worktree_path:
            evidence = {"category": "workspace_recovery_required", "checks": {"record": False}}
            self._store.record_workspace_event(
                run.run_id, "workspace_recovery_required", evidence
            )
            raise WorkspaceRecoveryRequired(
                "READY로 기록됐지만 branch나 경로가 없습니다.", {"record": False}
            )

        try:
            return self._planner.validate_existing(run.branch, run.worktree_path)
        except WorkspaceRecoveryRequired as error:
            self._store.record_workspace_event(
                run.run_id, "workspace_recovery_required", error.evidence()
            )
            raise

    def show(self, run_id: str) -> dict[str, Any]:
        """DB 기록과 실제 디스크 상태를 함께 돌려줍니다."""

        run = self._store.run(run_id)
        if run is None:
            raise RunError("run_not_found", f"{run_id}를 찾을 수 없습니다.")
        report: dict[str, Any] = {"run": run.to_dict(), "disk": None}
        if run.branch and run.worktree_path:
            report["disk"] = self._planner.inspect(run.branch, run.worktree_path)
        return report

    def cleanup(
        self, run_id: str, *, allow_dirty: bool = False, delete_branch: bool = False
    ) -> CleanupResult:
        """terminal Run의 worktree를 제거합니다.

        - branch는 기본적으로 보존합니다. 작업 내용이 남아 있을 수 있습니다.
        - dirty worktree는 `allow_dirty` 없이는 제거하지 않습니다.
        - Atlas가 만들었다고 DB가 증명하지 못하는 리소스는 건드리지 않습니다.
        - 실패는 숨기지 않고 event로 남깁니다.
        """

        run = self._store.run(run_id)
        if run is None:
            raise RunError("run_not_found", f"{run_id}를 찾을 수 없습니다.")
        if not run.status.is_terminal:
            raise WorkspaceError(
                "run_not_terminal",
                f"{run_id}가 아직 실행 중입니다. 종료 후 정리하세요.",
            )
        if not run.workspace_status.has_resources:
            return CleanupResult(run_id, False, True, "workspace가 이미 정리됐습니다.")
        if not run.branch or not run.worktree_path:
            return CleanupResult(run_id, False, True, "정리할 workspace 기록이 없습니다.")

        self._assert_owned(run)

        keep_branch = BRANCH_RETENTION.get(run.status, True) and not delete_branch
        try:
            self._planner.remove_worktree(run.worktree_path, allow_dirty=allow_dirty)
        except WorkspaceError as error:
            detail = {"category": error.category, "detail": error.message}
            self._store.record_workspace_event(run_id, "workspace_cleanup_failed", detail)
            return CleanupResult(run_id, False, True, error.message, error=detail)

        if not keep_branch:
            try:
                self._planner.delete_branch(run.branch, force=True)
            except WorkspaceError as error:
                detail = {"category": error.category, "detail": error.message}
                self._store.record_workspace_event(run_id, "workspace_cleanup_failed", detail)
                self._store.release_workspace(
                    run_id, branch_kept=True, reason="worktree만 제거했습니다."
                )
                return CleanupResult(run_id, True, True, error.message, error=detail)

        reason = (
            f"{run.status.value} Run의 worktree를 제거했습니다."
            f" branch는 {'보존' if keep_branch else '삭제'}했습니다."
        )
        self._store.release_workspace(run_id, branch_kept=keep_branch, reason=reason)
        return CleanupResult(run_id, True, keep_branch, reason)

    def inspect_all(self) -> list[dict[str, Any]]:
        """workspace를 가진 모든 Run의 디스크 상태를 확인합니다."""

        reports = []
        for run in self._store.runs_with_workspace():
            disk = None
            if run.branch and run.worktree_path:
                disk = self._planner.inspect(run.branch, run.worktree_path)
            reports.append({"run": run.to_dict(), "disk": disk})
        return reports

    def _assert_owned(self, run: Run) -> None:
        """Atlas가 만들었다고 증명할 수 있는 리소스만 다룹니다.

        증명은 두 가지가 함께 성립할 때만 인정합니다.

        1. branch가 Atlas namespace(`atlas/`)에 있습니다.
        2. DB에 이 Run이 그 branch와 경로를 만들었다는 기록이 있습니다.
        """

        if not is_atlas_branch(run.branch or ""):
            raise WorkspaceError(
                "branch_not_owned",
                f"Atlas namespace 밖의 branch입니다: {run.branch}",
            )
        owner = self._store.run_owning_branch(run.branch or "")
        if owner is None or owner.run_id != run.run_id:
            raise WorkspaceError(
                "provenance_mismatch",
                f"{run.branch}를 이 Run이 만들었다는 기록이 없습니다.",
            )
        try:
            Path(run.worktree_path or "").resolve().relative_to(
                self._planner.workspaces_root.resolve()
            )
        except (ValueError, OSError):
            raise WorkspaceError(
                "path_outside_root",
                f"worktree 경로가 worker root 밖입니다: {run.worktree_path}",
            ) from None
