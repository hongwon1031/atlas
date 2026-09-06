"""Executor process lifecycle orchestration.

DB 기록, safety gate, adapter 호출, heartbeat, cancellation을 엮습니다.

process spawn을 DB transaction 안에서 잡지 않으려고 단계를 나눕니다.

    reserve(Starting)  ->  spawn  ->  attach(Running)  ->  wait  ->  finish
                           실패 시 fail(Failed) + 남은 process 정리 시도

`Starting` 기록이 spawn보다 먼저 남으므로 중간에 죽어도 reconciliation이
"process를 만들려다 만 Run"을 식별할 수 있습니다.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import RunConfig
from .executor import (
    FAILURE_TO_RUN_CATEGORY,
    CancellationState,
    ExecutionStatus,
    ExecutorAdapter,
    ExecutorError,
    ExecutorFailure,
    ExecutorRequest,
    ExecutorResult,
    TerminationOutcome,
)
from .process_identity import IdentityVerdict, ProcessIdentity, verify
from .redaction import redact_argv, redact_line
from .schema import RunFailure, RunStatus, WorkspaceStatus
from .store import ExecutionConflict, ExecutionError, RunError, TaskStore, from_iso, to_iso, utcnow
from .workspace import WorkspaceError, WorkspaceRecoveryRequired
from .workspace_service import WorkspaceService

DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_GRACE_PERIOD_SECONDS = 5.0
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576


@dataclass(frozen=True)
class SafetyGateResult:
    """spawn 직전 재확인 결과."""

    passed: bool
    failed_checks: tuple[str, ...] = ()
    checks: dict[str, bool] = None  # type: ignore[assignment]

    def evidence(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed_checks": list(self.failed_checks),
            "checks": self.checks or {},
        }


@dataclass(frozen=True)
class ExecutionOutcome:
    execution_id: str
    started: bool
    result: ExecutorResult | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "started": self.started,
            "detail": self.detail,
            "result": self.result.to_dict() if self.result else None,
        }


class SafetyGateFailed(ExecutorError):
    def __init__(self, message: str, gate: SafetyGateResult) -> None:
        super().__init__("safety_gate", message)
        self.gate = gate


class ExecutionService:
    """Run 하나에 executor process를 붙입니다."""

    def __init__(
        self,
        store: TaskStore,
        adapter: ExecutorAdapter,
        workspaces: WorkspaceService,
        logs_root: Path | str,
        run_config: RunConfig | None = None,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._workspaces = workspaces
        self._logs_root = Path(logs_root)
        self._run_config = run_config or RunConfig()

    @property
    def adapter(self) -> ExecutorAdapter:
        return self._adapter

    def log_dir(self, run_id: str, execution_id: str) -> Path:
        """Run별 log 경로. Project boundary 밖으로 나가지 않게 조각을 정규화합니다."""

        safe_run = "".join(ch for ch in run_id if ch.isalnum() or ch in "-_")
        safe_exec = "".join(ch for ch in execution_id if ch.isalnum() or ch in "-_")
        candidate = (self._logs_root / (safe_run or "run") / (safe_exec or "exec")).resolve()
        root = self._logs_root.resolve()
        if not candidate.is_relative_to(root):
            raise ExecutorError("log_path_outside_root", "log 경로가 허용 범위를 벗어납니다.")
        return candidate

    # -- safety gate -----------------------------------------------------

    def safety_gate(self, run_id: str, worker_id: str, now: datetime | None = None) -> SafetyGateResult:
        """process를 띄우기 직전에 실행 근거가 아직 유효한지 다시 확인합니다.

        run-start나 workspace-create 이후에 승인이 회수되거나 claim이 풀렸을 수
        있습니다. 그 상태로 executor를 띄우면 승인 없는 side effect가 됩니다.
        """

        moment = now or utcnow()
        checks: dict[str, bool] = {
            "run_exists": False,
            "run_active": False,
            "workspace_ready": False,
            "workspace_valid": False,
            "task_approved": False,
            "claim_active": False,
            "claim_owner_matches": False,
            "lease_valid": False,
        }

        run = self._store.run(run_id)
        if run is None:
            return self._gate_result(checks)
        checks["run_exists"] = True
        checks["run_active"] = not run.status.is_terminal
        checks["workspace_ready"] = run.workspace_status is WorkspaceStatus.READY

        if checks["workspace_ready"]:
            try:
                self._workspaces.planner.validate_existing(run.branch or "", run.worktree_path or "")
                checks["workspace_valid"] = True
            except (WorkspaceRecoveryRequired, WorkspaceError):
                checks["workspace_valid"] = False

        task = self._store.task_by_fingerprint(run.fingerprint)
        checks["task_approved"] = bool(task and task["approved"] and task["is_current"])

        claim = self._store.claim_for(run.claim_id)
        if claim is not None and claim["released_at"] is None:
            checks["claim_active"] = True
            checks["claim_owner_matches"] = claim["lease_owner"] == worker_id
            checks["lease_valid"] = from_iso(claim["lease_expires_at"]) > moment

        return self._gate_result(checks)

    @staticmethod
    def _gate_result(checks: dict[str, bool]) -> SafetyGateResult:
        failed = tuple(name for name, ok in checks.items() if not ok)
        return SafetyGateResult(passed=not failed, failed_checks=failed, checks=checks)

    # -- lifecycle -------------------------------------------------------

    def start(
        self,
        run_id: str,
        worker_id: str,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        environment: dict[str, str] | None = None,
        secret_values: tuple[str, ...] = (),
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        grace_period_seconds: float = DEFAULT_GRACE_PERIOD_SECONDS,
        stdin_data: str = "",
    ) -> tuple[str, ExecutorRequest, Any]:
        """safety gate를 통과하면 process를 띄우고 `Running`으로 확정합니다.

        `(execution_id, request, handle)`을 돌려줍니다. 완료를 기다리지 않습니다.
        """

        run = self._store.run(run_id)
        if run is None:
            raise RunError("run_not_found", f"{run_id}를 찾을 수 없습니다.")

        gate = self.safety_gate(run_id, worker_id)
        if not gate.passed:
            self._store.record_execution_event(
                None, run_id, "execution_safety_gate_failed", gate.evidence()
            )
            raise SafetyGateFailed(
                f"실행 전 확인에 실패했습니다: {', '.join(gate.failed_checks)}", gate
            )

        request = ExecutorRequest(
            run_id=run_id,
            task_id=run.task_id,
            cwd=run.worktree_path or "",
            argv=tuple(argv),
            timeout_seconds=timeout_seconds,
            environment=dict(environment or {}),
            secret_values=tuple(secret_values),
            max_output_bytes=max_output_bytes,
            grace_period_seconds=grace_period_seconds,
            stdin_data=stdin_data,
        )

        try:
            execution_id = self._store.reserve_execution(
                run_id,
                task_id=run.task_id,
                executor_name=self._adapter.name,
                executor_provider=self._adapter.provider,
                worker_id=worker_id,
                cwd=request.cwd,
                command=redact_argv(request.argv, request.secret_values),
                timeout_seconds=timeout_seconds,
            )
        except ExecutionConflict as conflict:
            if conflict.category == "reservation_guard_failed":
                # 예약 transaction이 rollback되므로 그 안에서는 event를 남길 수
                # 없습니다. 밖에서 근거를 기록합니다.
                self._store.record_execution_event(
                    None,
                    run_id,
                    "execution_safety_gate_failed",
                    {
                        "stage": "reservation",
                        "failed_checks": (conflict.execution or {}).get("failed_checks", []),
                    },
                )
            raise

        # 예약과 spawn 사이에도 승인 회수나 claim 해제가 일어날 수 있습니다.
        # spawn 직전에 마지막으로 한 번 더 확인하고, 실패하면 process를 만들지
        # 않고 예약을 명시적으로 정리합니다.
        final_gate = self.safety_gate(run_id, worker_id)
        if not final_gate.passed:
            self._store.finish_execution(
                execution_id,
                status=ExecutionStatus.FAILED,
                failure_category=ExecutorFailure.SAFETY_GATE.value,
                error={
                    "category": "final_safety_gate_failed",
                    "failed_checks": list(final_gate.failed_checks),
                },
            )
            self._store.record_execution_event(
                execution_id, run_id, "execution_safety_gate_failed",
                {"stage": "final", **final_gate.evidence()},
            )
            raise SafetyGateFailed(
                f"spawn 직전 확인에 실패했습니다: {', '.join(final_gate.failed_checks)}",
                final_gate,
            )

        try:
            handle = self._adapter.spawn(request, self.log_dir(run_id, execution_id))
        except ExecutorError as error:
            self._store.finish_execution(
                execution_id,
                status=ExecutionStatus.FAILED,
                failure_category=ExecutorFailure.SPAWN_FAILED.value,
                error={"category": error.category, "detail": redact_line(error.message)},
            )
            raise

        self._store.attach_process(
            execution_id,
            pid=handle.pid,
            identity=handle.identity.to_dict(),
            started_at=handle.started_at,
        )
        return execution_id, request, handle

    def wait(
        self, execution_id: str, request: ExecutorRequest, handle: Any
    ) -> ExecutorResult:
        """완료를 기다리며 Run heartbeat를 유지합니다."""

        stop = threading.Event()
        failures: list[str] = []
        beat = threading.Thread(
            target=self._heartbeat_loop,
            args=(request.run_id, stop, failures),
            daemon=True,
        )
        beat.start()
        try:
            result = self._adapter.wait(handle, request)
        finally:
            stop.set()
            beat.join(timeout=10.0)

        if failures:
            # heartbeat 실패를 무시하지 않습니다. reconciliation이 이 Run을
            # stale로 볼 수 있으므로 근거를 남깁니다.
            self._store.record_execution_event(
                execution_id,
                request.run_id,
                "execution_heartbeat_failed",
                {"failures": failures[:5], "count": len(failures)},
            )

        if result.process_may_be_alive:
            # 종료를 확인하지 못했습니다. Finished로 확정하면 살아 있는 process가
            # reconciliation 대상에서 빠집니다. Cancelling을 유지해 active로 남기고
            # 근거를 남깁니다.
            self._store.set_cancellation_state(
                execution_id,
                CancellationState.UNCONFIRMED.value,
                reason="termination_unverified",
                status=ExecutionStatus.CANCELLING,
            )
            self._store.record_execution_event(
                execution_id,
                request.run_id,
                "execution_termination_unverified",
                {
                    "failure": result.failure.value if result.failure else None,
                    "termination": result.termination.value,
                    **result.termination_evidence,
                },
            )
            return result

        self._store.finish_execution(
            execution_id,
            status=ExecutionStatus.FINISHED,
            exit_code=result.exit_code,
            finished_at=result.finished_at,
            stdout=result.stdout.to_dict() if result.stdout else None,
            stderr=result.stderr.to_dict() if result.stderr else None,
            failure_category=result.failure.value if result.failure else None,
            error={"detail": redact_line(result.detail, secrets=request.secret_values)}
            if result.detail
            else None,
            cancellation_state=result.cancellation_state.value,
        )
        return result

    def run(
        self, run_id: str, worker_id: str, argv: tuple[str, ...], **kwargs: Any
    ) -> ExecutionOutcome:
        """start + wait. CLI가 쓰는 동기 실행 경로입니다."""

        execution_id, request, handle = self.start(run_id, worker_id, argv, **kwargs)
        result = self.wait(execution_id, request, handle)
        return ExecutionOutcome(execution_id=execution_id, started=True, result=result)

    def apply_to_run(self, run_id: str, result: ExecutorResult) -> None:
        """execution 결과를 Run status로 옮깁니다.

        Run status와 Task status를 동일시하지 않습니다. Run이 Succeeded여도 Task는
        사람 승인과 merge 전까지 Completed가 아닙니다.
        """

        run = self._store.run(run_id)
        if run is None or run.status.is_terminal:
            return

        if result.succeeded:
            self._store.finish_run(run_id, RunStatus.SUCCEEDED)
            return

        failure = result.failure or ExecutorFailure.UNKNOWN
        category = FAILURE_TO_RUN_CATEGORY.get(failure, "unknown")
        status = (
            RunStatus.CANCELLED if failure is ExecutorFailure.CANCELLED else RunStatus.FAILED
        )
        self._store.finish_run(
            run_id,
            status,
            failure=RunFailure(category, redact_line(result.detail) or failure.value),
        )

    # -- cancellation ----------------------------------------------------

    def cancel(
        self, run_id: str, reason: str, grace_period_seconds: float = DEFAULT_GRACE_PERIOD_SECONDS
    ) -> dict[str, Any]:
        """active execution을 취소합니다. 이미 끝났으면 idempotent합니다."""

        row = self._store.active_execution(run_id)
        if row is None:
            return {"run_id": run_id, "cancelled": False, "detail": "active execution이 없습니다."}

        execution_id = row["execution_id"]
        self._store.set_cancellation_state(
            execution_id,
            CancellationState.REQUESTED.value,
            reason=reason,
            status=ExecutionStatus.CANCELLING,
        )

        identity = self._identity_of(row)
        if identity is None:
            self._store.finish_execution(
                execution_id,
                status=ExecutionStatus.FINISHED,
                failure_category=ExecutorFailure.CANCELLED.value,
                cancellation_state=CancellationState.COMPLETED.value,
                error={"detail": "process가 기록되기 전에 취소됐습니다."},
            )
            return {"run_id": run_id, "cancelled": True, "detail": "process 없이 취소했습니다."}

        verdict = verify(identity)
        if verdict is IdentityVerdict.PROCESS_ABSENT:
            self._store.finish_execution(
                execution_id,
                status=ExecutionStatus.FINISHED,
                failure_category=ExecutorFailure.CANCELLED.value,
                cancellation_state=CancellationState.COMPLETED.value,
                error={"detail": "process가 이미 종료됐습니다."},
            )
            return {"run_id": run_id, "cancelled": True, "detail": "이미 종료된 process입니다."}

        if not verdict.may_terminate:
            # identity를 증명하지 못하면 절대 종료하지 않습니다.
            self._store.record_execution_event(
                execution_id,
                run_id,
                "execution_cancel_refused",
                {"verdict": verdict.value, "reason": reason},
            )
            raise ExecutorError(
                "identity_unverified",
                f"process identity를 확인하지 못해 종료하지 않습니다: {verdict.value}",
            )

        from .executor import ProcessHandle

        handle = ProcessHandle(
            pid=identity.pid,
            identity=identity,
            started_at=row["process_started_at"] or "",
            process_group_id=identity.pid,
        )
        terminate = getattr(self._adapter, "terminate", None)
        if terminate is None:
            state = self._adapter.cancel(handle, grace_period_seconds)
            outcome, evidence = TerminationOutcome.CONFIRMED, {}
        else:
            state, outcome, evidence = terminate(handle, grace_period_seconds)

        if outcome.process_may_be_alive:
            # 종료를 확인하지 못했습니다. terminal로 확정하지 않습니다.
            self._store.set_cancellation_state(
                execution_id,
                CancellationState.UNCONFIRMED.value,
                reason=reason,
                status=ExecutionStatus.CANCELLING,
            )
            self._store.record_execution_event(
                execution_id, run_id, "execution_termination_unverified",
                {"reason": redact_line(reason), "termination": outcome.value, **evidence},
            )
            return {
                "run_id": run_id,
                "cancelled": False,
                "detail": "종료를 확인하지 못했습니다. reconciliation 대상으로 남깁니다.",
                "termination": outcome.value,
            }

        self._store.finish_execution(
            execution_id,
            status=ExecutionStatus.FINISHED,
            failure_category=ExecutorFailure.CANCELLED.value,
            cancellation_state=state.value,
            error={"detail": redact_line(reason)},
        )
        return {"run_id": run_id, "cancelled": True, "detail": state.value}

    def cancel_for_lost_authorization(self, worker_id: str) -> list[dict[str, Any]]:
        """승인 회수나 claim 상실로 실행 근거를 잃은 execution을 취소합니다.

        polling이 승인을 회수하거나 claim이 풀린 뒤에도 executor는 계속 살아
        있습니다. 이 함수가 그 간극을 닫습니다.
        """

        cancelled: list[dict[str, Any]] = []
        for row in self._store.active_executions():
            gate = self.safety_gate(row["run_id"], worker_id)
            if gate.passed:
                continue
            reason = f"실행 근거 상실: {', '.join(gate.failed_checks)}"
            try:
                outcome = self.cancel(row["run_id"], reason)
            except ExecutorError as error:
                outcome = {
                    "run_id": row["run_id"],
                    "cancelled": False,
                    "detail": error.message,
                }
            outcome["failed_checks"] = list(gate.failed_checks)
            cancelled.append(outcome)
        return cancelled

    # -- reads -----------------------------------------------------------

    def show(self, run_id: str) -> dict[str, Any]:
        rows = self._store.executions(run_id=run_id, limit=10)
        payload = []
        for row in rows:
            identity = self._identity_of(row)
            payload.append(
                {
                    "execution_id": row["execution_id"],
                    "status": row["status"],
                    "executor_name": row["executor_name"],
                    "process_id": row["process_id"],
                    "process_started_at": row["process_started_at"],
                    "process_finished_at": row["process_finished_at"],
                    "process_exit_code": row["process_exit_code"],
                    "cancellation_state": row["cancellation_state"],
                    "failure_category": row["failure_category"],
                    "stdout_bytes": row["stdout_bytes"],
                    "stderr_bytes": row["stderr_bytes"],
                    "identity_verdict": verify(identity).value if identity else "process_absent",
                }
            )
        return {"run_id": run_id, "count": len(payload), "executions": payload}

    @staticmethod
    def _identity_of(row: Any) -> ProcessIdentity | None:
        raw = row["process_identity"]
        if not raw:
            return None
        try:
            return ProcessIdentity.from_dict(json.loads(raw))
        except (ValueError, KeyError, TypeError):
            return None

    def _heartbeat_loop(self, run_id: str, stop: threading.Event, failures: list[str]) -> None:
        """process가 사는 동안 Run heartbeat를 유지합니다.

        heartbeat가 끊기면 reconciliation이 이 Run을 stale로 판정합니다. 따라서
        process lifecycle과 heartbeat lifecycle을 함께 묶습니다.

        SQLite 연결은 스레드 간에 공유할 수 없으므로 이 스레드가 자기 연결을
        엽니다. 실패는 삼키지 않고 `failures`에 남겨 호출자가 event로 기록합니다.
        """

        interval = max(1.0, self._run_config.heartbeat_interval_seconds)
        try:
            beat_store = TaskStore(self._store.path)
        except Exception as error:  # noqa: BLE001 - 어떤 실패든 근거를 남깁니다.
            failures.append(f"open_failed:{type(error).__name__}")
            return

        try:
            worker = None
            while not stop.wait(0 if worker is None else interval):
                try:
                    run = beat_store.run(run_id)
                except Exception as error:  # noqa: BLE001
                    failures.append(f"read_failed:{type(error).__name__}")
                    return
                if run is None or run.status.is_terminal:
                    return
                worker = run.worker_id
                try:
                    beat_store.heartbeat(run_id, worker)
                except (RunError, ExecutionError) as error:
                    failures.append(getattr(error, "category", "unknown"))
                    return
                except Exception as error:  # noqa: BLE001
                    failures.append(f"beat_failed:{type(error).__name__}")
                    return
        finally:
            beat_store.close()
