"""Restart reconciliation.

docs/specs/execution-runtime.md의 "Restart and Recovery"를 구현합니다.

핵심 요구는 두 가지입니다.

- lease 만료나 heartbeat 중단만으로 즉시 재실행하지 않습니다.
- process 상태를 증명할 수 없으면 새 side effect를 허용하지 않고 recovery
  review 상태로 기록합니다. 여기서는 `Orphaned`입니다.

process identity(PID, start time) 확인은 executor process를 만드는 후속
slice의 범위입니다. 지금은 heartbeat와 claim lease만으로 판단하며, 판단 근거를
event에 남겨 나중에 감사할 수 있게 합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from .config import RunConfig
from .execution_service import ExecutionService
from .process_identity import IdentityVerdict, ProcessIdentity, verify
from .schema import Run, RunFailure, RunStatus, WorkspaceStatus
from .store import TaskStore, from_iso, utcnow
from .workspace_service import WorkspaceService

# 재실행이 아니라 사람 확인이 필요하다는 뜻의 실패 분류입니다.
ORPHAN_FAILURE_CATEGORY = "worker_lost"


@dataclass(frozen=True)
class RunVerdict:
    """Run 하나에 대한 판정과 근거."""

    run_id: str
    task_id: str
    action: str
    reason: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "action": self.action,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ReconcileReport:
    checked: int = 0
    healthy: tuple[str, ...] = ()
    orphaned: tuple[str, ...] = ()
    verdicts: tuple[RunVerdict, ...] = ()
    workspace_findings: tuple[dict[str, Any], ...] = ()
    process_findings: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "healthy": list(self.healthy),
            "orphaned": list(self.orphaned),
            "verdicts": [verdict.to_dict() for verdict in self.verdicts],
            "workspace_findings": list(self.workspace_findings),
            "process_findings": list(self.process_findings),
        }


@dataclass
class _Counters:
    healthy: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    verdicts: list[RunVerdict] = field(default_factory=list)


class RunReconciler:
    """active Run을 훑어 stale한 것을 recovery review로 넘깁니다."""

    def __init__(
        self,
        store: TaskStore,
        config: RunConfig | None = None,
        workspaces: WorkspaceService | None = None,
        executions: ExecutionService | None = None,
    ) -> None:
        self._store = store
        self._config = config or RunConfig()
        self._workspaces = workspaces
        self._executions = executions

    @property
    def config(self) -> RunConfig:
        return self._config

    def reconcile(self, now: datetime | None = None) -> ReconcileReport:
        moment = now or utcnow()
        counters = _Counters()
        active = self._store.active_runs()
        workspace_findings = self.reconcile_workspaces(now=moment)
        process_findings = self.reconcile_processes(now=moment)

        for run in active:
            verdict = self.evaluate(run, moment)

            if verdict.action != "orphan":
                counters.verdicts.append(verdict)
                counters.healthy.append(run.run_id)
                continue

            # 판정과 전이 사이에 heartbeat가 도착할 수 있습니다. store가 하나의
            # transaction 안에서 다시 확인하고, 조건이 깨졌으면 회수하지 않습니다.
            orphaned = self._store.orphan_if_stale(
                run.run_id,
                observed_heartbeat_at=run.heartbeat_at,
                stale_after_seconds=self._config.stale_after_seconds,
                failure=RunFailure(ORPHAN_FAILURE_CATEGORY, verdict.reason),
                evidence=verdict.evidence,
                now=moment,
            )
            if orphaned is None:
                counters.verdicts.append(
                    replace(
                        verdict,
                        action="keep",
                        reason="판정 이후 Run 상태가 바뀌어 회수하지 않았습니다.",
                        evidence={**verdict.evidence, "revalidated": True},
                    )
                )
                counters.healthy.append(run.run_id)
            else:
                counters.verdicts.append(verdict)
                counters.orphaned.append(run.run_id)

        return ReconcileReport(
            checked=len(active),
            healthy=tuple(counters.healthy),
            orphaned=tuple(counters.orphaned),
            verdicts=tuple(counters.verdicts),
            workspace_findings=tuple(workspace_findings),
            process_findings=tuple(process_findings),
        )

    def reconcile_processes(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """기록된 executor process가 실제 상태와 맞는지 확인합니다.

        PID 존재 여부만 보지 않습니다. 저장한 identity와 현재 같은 PID의 identity가
        일치해야 우리 process로 인정합니다. 일치하지 않으면 다른 process일 수
        있으므로 **절대 종료하지 않습니다.**

        이 slice에서는 자동 재실행도 하지 않습니다. 판정과 근거 기록만 합니다.
        """

        findings: list[dict[str, Any]] = []

        for row in self._store.active_executions():
            identity = ExecutionService._identity_of(row)
            verdict = verify(identity) if identity else IdentityVerdict.PROCESS_ABSENT
            status = row["status"]

            if identity is None:
                # Starting에서 attach 전에 죽었습니다.
                kind = "execution_recovery_required"
                problem = "process_never_attached"
            elif verdict is IdentityVerdict.MATCH:
                findings.append(
                    self._process_finding(row, "execution_healthy", "process_alive", verdict, now)
                )
                continue
            elif verdict is IdentityVerdict.PROCESS_ABSENT:
                kind = "execution_recovery_required"
                problem = "process_missing"
            elif verdict is IdentityVerdict.MISMATCH:
                # PID는 살아 있지만 다른 process입니다. 종료 금지.
                kind = "execution_recovery_required"
                problem = "pid_identity_mismatch"
            else:
                kind = "execution_recovery_required"
                problem = "identity_unverifiable"

            findings.append(self._process_finding(row, kind, problem, verdict, now, status))

        for row in self._store.executions_for_terminal_runs():
            identity = ExecutionService._identity_of(row)
            verdict = verify(identity) if identity else IdentityVerdict.PROCESS_ABSENT
            if verdict is IdentityVerdict.PROCESS_ABSENT:
                continue
            # Run은 끝났는데 process가 살아 있습니다. 승인 근거 없이 side effect를
            # 만들 수 있으므로 심각도가 높습니다. 다만 ownership을 증명하기 전에
            # 자동 종료하지 않습니다.
            findings.append(
                self._process_finding(
                    row, "execution_surviving_terminal_run", "process_outlived_run", verdict, now
                )
            )

        return findings

    def _process_finding(
        self,
        row: Any,
        kind: str,
        problem: str,
        verdict: IdentityVerdict,
        now: datetime | None,
        status: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "run_id": row["run_id"],
            "execution_id": row["execution_id"],
            "kind": kind,
            "problem": problem,
            "execution_status": status or row["status"],
            "identity_verdict": verdict.value,
            "may_terminate": verdict.may_terminate,
            "process_id": row["process_id"],
        }
        if kind != "execution_healthy":
            self._store.record_execution_event(
                row["execution_id"], row["run_id"], kind, payload, now=now
            )
        return payload

    def reconcile_workspaces(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """기록된 workspace가 디스크와 일치하는지 확인합니다.

        불일치를 발견해도 임의로 복구하거나 삭제하지 않습니다. recovery-required
        근거를 event로 남기고 사람이 판단하게 합니다.
        """

        if self._workspaces is None:
            return []

        findings: list[dict[str, Any]] = []
        for run in self._store.runs_with_workspace():
            if not run.branch or not run.worktree_path:
                continue
            try:
                disk = self._workspaces.planner.inspect(run.branch, run.worktree_path)
            except Exception as error:  # noqa: BLE001 - 진단 실패도 근거로 남깁니다.
                findings.append(
                    self._workspace_finding(
                        run, "workspace_inspect_failed", {"detail": type(error).__name__}, now
                    )
                )
                continue

            problems = []
            if run.workspace_status is WorkspaceStatus.READY:
                if not disk["path_exists"]:
                    problems.append("worktree_missing")
                elif not disk["registered_worktree"]:
                    problems.append("worktree_not_registered")
                if disk["branch_matches"] is False:
                    problems.append("branch_mismatch")
                if not disk["branch_exists"]:
                    problems.append("branch_missing")
            elif run.workspace_status in (WorkspaceStatus.PREPARING, WorkspaceStatus.FAILED):
                if disk["path_exists"] or disk["branch_exists"]:
                    problems.append("incomplete_workspace_left_resources")

            if not disk["path_within_root"] and disk["path_exists"]:
                problems.append("path_outside_root")

            if problems:
                findings.append(
                    self._workspace_finding(
                        run,
                        "workspace_recovery_required",
                        {"problems": problems, "disk": disk},
                        now,
                    )
                )
        return findings

    def _workspace_finding(
        self, run: Run, kind: str, detail: dict[str, Any], now: datetime | None
    ) -> dict[str, Any]:
        payload = {
            "run_id": run.run_id,
            "task_id": run.task_id,
            "kind": kind,
            "workspace_status": run.workspace_status.value,
            "branch": run.branch,
            **detail,
        }
        self._store.record_workspace_event(run.run_id, kind, payload, now=now)
        return payload

    def evaluate(self, run: Run, now: datetime | None = None) -> RunVerdict:
        """Run 하나를 판정합니다. 상태를 바꾸지 않으므로 단독 조회에 쓸 수 있습니다.

        판정은 snapshot 기반입니다. 실제 회수는 `TaskStore.orphan_if_stale`이
        transaction 안에서 조건을 다시 확인한 뒤에만 수행합니다.
        """

        moment = now or utcnow()
        heartbeat_at = from_iso(run.heartbeat_at)
        heartbeat_age = (moment - heartbeat_at).total_seconds()
        deadline = heartbeat_at + timedelta(seconds=self._config.stale_after_seconds)

        claim = self._store.claim_for(run.claim_id)
        claim_released = claim is None or claim["released_at"] is not None
        lease_expired = (
            claim is not None and from_iso(claim["lease_expires_at"]) <= moment
        )

        evidence: dict[str, Any] = {
            "status": run.status.value,
            "worker_id": run.worker_id,
            "heartbeat_at": run.heartbeat_at,
            "heartbeat_age_seconds": round(heartbeat_age, 3),
            "stale_after_seconds": self._config.stale_after_seconds,
            "claim_id": run.claim_id,
            "claim_released": claim_released,
            "lease_expired": lease_expired,
            # process identity 확인은 executor process가 생긴 뒤에 가능합니다.
            "process_identity_checked": False,
        }

        heartbeat_stale = deadline <= moment
        if not heartbeat_stale:
            return RunVerdict(
                run.run_id,
                run.task_id,
                "keep",
                "heartbeat가 stale threshold 안에 있습니다.",
                evidence,
            )

        # heartbeat가 끊겼고 claim 근거도 사라졌거나 만료됐습니다. 이 Run을
        # 계속 살아 있다고 볼 근거가 없으므로 recovery review로 넘깁니다.
        if claim_released:
            reason = "heartbeat가 중단됐고 claim이 이미 해제됐습니다."
        elif lease_expired:
            reason = "heartbeat가 중단됐고 claim lease도 만료됐습니다."
        else:
            reason = "heartbeat가 stale threshold를 초과했습니다."

        return RunVerdict(run.run_id, run.task_id, "orphan", reason, evidence)


def is_stale(run: Run, config: RunConfig, now: datetime | None = None) -> bool:
    moment = now or utcnow()
    return (
        not RunStatus(run.status).is_terminal
        and from_iso(run.heartbeat_at) + timedelta(seconds=config.stale_after_seconds) <= moment
    )
