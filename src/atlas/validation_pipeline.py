"""AwaitingValidation Run을 검증하고 Succeeded 또는 Failed로 확정합니다.

executor runtime과 같은 원칙을 씁니다.

- DB 전이와 subprocess spawn을 같은 transaction에 넣지 않습니다.
- 시작 전에 근거를 확인하고, 예약 transaction 안에서 다시 확인합니다.
- process identity를 저장하고, 증명하지 못한 process는 종료하지 않습니다.
- 출력은 상한 있는 redacted artifact로 저장합니다.
- 불일치를 발견하면 기록만 하고 자동으로 복구하지 않습니다.

provider를 모릅니다. Claude가 만든 변경이든 사람이 만든 변경이든 같습니다.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RunConfig
from .executor import ExecutorError, ExecutorFailure, ExecutorRequest
from .gitcmd import GitError
from .local_process import LocalProcessExecutor, read_log_tail
from .redaction import redact_argv, redact_line
from .schema import RunFailure, RunStatus
from .store import RunError, TaskStore, from_iso, to_iso, utcnow
from .validation_models import (
    VALIDATION_TO_RUN_CATEGORY,
    StepKind,
    StepStatus,
    ValidationFailure,
    ValidationOutcome,
    ValidationPlan,
    ValidationReport,
    ValidationStatus,
    ValidationStep,
    ValidationStepResult,
    decide,
)
from .validation_plan import build_plan
from .workspace import WorkspaceRecoveryRequired
from .workspace_service import WorkspaceService
from .worktree_changes import WorktreeState, compare, safe_capture

# validation process의 executor 이름. AI executor와 구분해야 감사 기록에서
# "누가 무엇을 했는지"가 섞이지 않습니다.
VALIDATION_EXECUTOR_NAME = "validation_local"
VALIDATION_EXECUTOR_PROVIDER = "local"

DEFAULT_STEP_TIMEOUT_SECONDS = 900.0
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576

# event에 남길 출력 요약 길이. 전체 stdout/stderr를 저장하지 않습니다.
OUTPUT_SUMMARY_CHARS = 300


class ValidationGateFailed(RunError):
    """validation을 시작할 근거가 없습니다."""

    def __init__(self, message: str, checks: dict[str, bool]) -> None:
        super().__init__(ValidationFailure.GATE_FAILED.value, message)
        self.checks = checks

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(name for name, ok in self.checks.items() if not ok)


@dataclass(frozen=True)
class GateResult:
    checks: dict[str, bool]

    @property
    def passed(self) -> bool:
        return all(self.checks.values())

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(name for name, ok in self.checks.items() if not ok)

    def to_dict(self) -> dict[str, Any]:
        return {"checks": dict(self.checks), "failed_checks": list(self.failed_checks)}


class ValidationPipeline:
    """Run 하나의 validation을 수행합니다."""

    def __init__(
        self,
        store: TaskStore,
        workspaces: WorkspaceService,
        logs_root: Path | str,
        config: RunConfig | None = None,
        runtime: LocalProcessExecutor | None = None,
        git_timeout_seconds: float = 30.0,
    ) -> None:
        self._store = store
        self._workspaces = workspaces
        self._logs_root = Path(logs_root)
        self._config = config or RunConfig()
        self._runtime = runtime or LocalProcessExecutor(
            name=VALIDATION_EXECUTOR_NAME, provider=VALIDATION_EXECUTOR_PROVIDER
        )
        self._git_timeout = git_timeout_seconds

    # -- gate ------------------------------------------------------------

    def gate(self, run_id: str, worker_id: str, now=None) -> GateResult:
        """validation을 시작해도 되는지 확인합니다.

        executor의 safety gate와 같은 근거를 봅니다. 승인과 claim이 살아 있고,
        workspace가 지금도 유효해야 합니다.
        """

        moment = now or utcnow()
        checks: dict[str, bool] = {
            "run_exists": False,
            "run_awaiting_validation": False,
            "workspace_ready": False,
            "workspace_valid": False,
            "task_approved": False,
            "claim_active": False,
            "claim_owner_matches": False,
            "lease_valid": False,
            "no_active_execution": False,
        }

        run = self._store.run(run_id)
        if run is None:
            return GateResult(checks)
        checks["run_exists"] = True
        checks["run_awaiting_validation"] = run.status is RunStatus.AWAITING_VALIDATION
        checks["workspace_ready"] = (
            run.workspace_status.value == "ready" and bool(run.worktree_path)
        )
        checks["no_active_execution"] = self._store.active_execution(run_id) is None

        if checks["workspace_ready"]:
            try:
                self._workspaces.planner.validate_existing(run.branch, run.worktree_path)
                checks["workspace_valid"] = True
            except (WorkspaceRecoveryRequired, GitError, OSError):
                checks["workspace_valid"] = False

        task = self._store.task_by_fingerprint(run.fingerprint)
        checks["task_approved"] = bool(task and task["approved"] and task["is_current"])

        claim = self._store.claim_for(run.claim_id)
        if claim is not None and claim["released_at"] is None:
            checks["claim_active"] = True
            checks["claim_owner_matches"] = claim["lease_owner"] == worker_id
            checks["lease_valid"] = from_iso(claim["lease_expires_at"]) > moment

        return GateResult(checks)

    # -- 실행 ------------------------------------------------------------

    def log_dir(self, run_id: str, validation_id: str) -> Path:
        return self._logs_root / run_id / validation_id

    def plan_for(self, run_id: str, timeout_seconds: float | None = None) -> ValidationPlan:
        run = self._store.run(run_id)
        if run is None or not run.worktree_path:
            raise RunError("run_not_found", f"{run_id}의 worktree를 찾을 수 없습니다.")
        return build_plan(
            run.worktree_path,
            timeout_seconds=timeout_seconds or DEFAULT_STEP_TIMEOUT_SECONDS,
        )

    def validate(
        self,
        run_id: str,
        worker_id: str,
        *,
        timeout_seconds: float | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> ValidationReport:
        """validation을 한 번 수행하고 Run을 확정합니다."""

        gate = self.gate(run_id, worker_id)
        if not gate.passed:
            self._store.record_validation_event(
                None, run_id, "validation_gate_failed", {"stage": "initial", **gate.to_dict()}
            )
            raise ValidationGateFailed(
                f"validation 시작 전 확인에 실패했습니다: {', '.join(gate.failed_checks)}",
                gate.checks,
            )

        run = self._store.run(run_id)
        plan = build_plan(
            run.worktree_path, timeout_seconds=timeout_seconds or DEFAULT_STEP_TIMEOUT_SECONDS
        )

        validation_id = self._store.start_validation(
            run_id, worker_id=worker_id, cwd=run.worktree_path, plan=plan.to_dict()
        )

        stop = threading.Event()
        failures: list[str] = []
        beat = threading.Thread(
            target=self._heartbeat_loop, args=(run_id, worker_id, stop, failures), daemon=True
        )
        beat.start()
        try:
            report = self._execute_plan(
                validation_id, run, worker_id, plan, max_output_bytes
            )
        except BaseException:
            # 결과를 저장하기 전에 중단됐습니다. 자동으로 성공 처리하지
            # 않습니다. ambiguous로 남겨 사람이 판단합니다.
            self._store.finish_validation(
                validation_id,
                status=ValidationStatus.FAILED,
                outcome=ValidationOutcome.AMBIGUOUS.value,
                summary="결과를 저장하기 전에 중단됐습니다.",
                failure_category=ValidationFailure.STATE_AMBIGUOUS.value,
            )
            raise
        finally:
            stop.set()
            beat.join(timeout=10.0)

        if failures:
            self._store.record_validation_event(
                validation_id,
                run_id,
                "validation_heartbeat_failed",
                {"failures": failures[:5], "count": len(failures)},
            )

        self._persist(validation_id, report)
        self._apply_to_run(run_id, worker_id, report)
        return report

    def _execute_plan(
        self,
        validation_id: str,
        run,
        worker_id: str,
        plan: ValidationPlan,
        max_output_bytes: int,
    ) -> ValidationReport:
        self._store.mark_validation_running(validation_id)
        results: list[ValidationStepResult] = []

        for position, step in enumerate(plan.steps):
            result = self._run_step(
                validation_id, run, worker_id, step, position, max_output_bytes
            )
            results.append(result)
            if result.blocks_success:
                # required step이 막혔습니다. 남은 step을 계속 돌려도 결론이
                # 바뀌지 않고 시간만 씁니다.
                for skipped_position in range(position + 1, len(plan.steps)):
                    remaining = plan.steps[skipped_position]
                    results.append(
                        self._record_result(
                            validation_id,
                            run.run_id,
                            skipped_position,
                            ValidationStepResult(
                                name=remaining.name,
                                kind=remaining.kind,
                                required=remaining.required,
                                status=StepStatus.SKIPPED,
                                argv=remaining.argv,
                                reason="earlier_required_step_failed",
                                evidence=remaining.evidence,
                            ),
                        )
                    )
                break

        outcome, failure, warnings = decide(results, plan)
        summary = self._summarize(results, outcome, warnings)
        return ValidationReport(
            validation_id=validation_id,
            run_id=run.run_id,
            outcome=outcome,
            results=tuple(results),
            plan=plan,
            failure=failure,
            summary=summary,
            warnings=warnings,
        )

    def _run_step(
        self,
        validation_id: str,
        run,
        worker_id: str,
        step: ValidationStep,
        position: int,
        max_output_bytes: int,
    ) -> ValidationStepResult:
        if step.skip_reason:
            return self._record_result(
                validation_id,
                run.run_id,
                position,
                ValidationStepResult(
                    name=step.name,
                    kind=step.kind,
                    required=step.required,
                    status=StepStatus.SKIPPED,
                    reason=step.skip_reason,
                    evidence=step.evidence,
                ),
            )
        if step.error_reason:
            return self._record_result(
                validation_id,
                run.run_id,
                position,
                ValidationStepResult(
                    name=step.name,
                    kind=step.kind,
                    required=step.required,
                    status=StepStatus.ERROR,
                    reason=step.error_reason,
                    evidence=step.evidence,
                ),
            )
        if step.kind is StepKind.WORKSPACE_INTEGRITY:
            return self._record_result(
                validation_id, run.run_id, position, self._workspace_step(run, step)
            )
        if step.kind is StepKind.GIT_POLICY:
            return self._record_result(
                validation_id, run.run_id, position, self._git_policy_step(run, step)
            )
        return self._command_step(
            validation_id, run, worker_id, step, position, max_output_bytes
        )

    # -- 내부 step -------------------------------------------------------

    def _workspace_step(self, run, step: ValidationStep) -> ValidationStepResult:
        """실행 직전에 workspace 경계를 다시 확인합니다.

        gate에서 한 번 봤지만 그 사이에 바뀔 수 있습니다. 잘못된 경로에서
        검증을 돌리면 결과 자체가 무의미합니다.
        """

        started = to_iso(utcnow())
        try:
            report = self._workspaces.planner.validate_existing(
                run.branch, run.worktree_path
            )
            checks = report.get("checks", {})
            status = StepStatus.PASSED
            reason = ""
        except WorkspaceRecoveryRequired as error:
            checks = error.checks
            status = StepStatus.FAILED
            reason = "workspace_invalid"
        except (GitError, OSError) as error:
            checks = {}
            status = StepStatus.ERROR
            reason = f"workspace_unreadable:{type(error).__name__}"

        return ValidationStepResult(
            name=step.name,
            kind=step.kind,
            required=step.required,
            status=status,
            started_at=started,
            finished_at=to_iso(utcnow()),
            reason=reason,
            evidence={
                "checks": checks,
                "worktree_path": run.worktree_path,
                "branch": run.branch,
            },
        )

    def _git_policy_step(self, run, step: ValidationStep) -> ValidationStepResult:
        """구현 이후 repository 상태가 정책을 지키는지 확인합니다.

        PR #11의 변경 감지기를 재사용합니다. 구현 시점의 evidence와 지금 상태를
        비교해, 사람이 중간에 worktree를 건드린 경우를 조용히 통과시키지
        않습니다.
        """

        started = to_iso(utcnow())
        task = self._task_for(run)
        state = safe_capture(run.worktree_path, self._git_timeout)
        if state is None:
            return ValidationStepResult(
                name=step.name,
                kind=step.kind,
                required=step.required,
                status=StepStatus.ERROR,
                started_at=started,
                finished_at=to_iso(utcnow()),
                reason="git_state_unreadable",
                evidence={},
            )

        # 기준선은 workspace를 만든 시점입니다. base revision에 깨끗한 트리로
        # 시작했으므로, 지금 상태와의 차이가 곧 이번 Run이 만든 변경입니다.
        # 현재 상태를 자기 자신과 비교하면 아무 변경도 보이지 않습니다.
        baseline_state = WorktreeState(
            head=run.base_revision or state.head, branch=run.branch, entries=()
        )
        changes = compare(baseline_state, state, task, process_succeeded=True)
        baseline = self._implementation_baseline(run.run_id)

        drift = self._detect_drift(baseline, changes)
        status = StepStatus.PASSED
        reason = ""
        if changes.violations:
            status = StepStatus.FAILED
            reason = ",".join(changes.violations)
        elif drift:
            # 구현 이후 누군가 worktree를 바꿨습니다. 조용히 통과시키지
            # 않습니다.
            status = StepStatus.FAILED
            reason = "workspace_changed_after_implementation"
        elif not changes.has_changes:
            status = StepStatus.FAILED
            reason = "no_changes_to_validate"

        return ValidationStepResult(
            name=step.name,
            kind=step.kind,
            required=step.required,
            status=status,
            started_at=started,
            finished_at=to_iso(utcnow()),
            reason=reason,
            evidence={
                "changes": changes.to_dict(),
                "implementation_baseline": baseline.get("changed_files") if baseline else None,
                "drift": drift,
            },
        )

    def _detect_drift(self, baseline: dict[str, Any] | None, changes) -> list[str]:
        """구현 시점 evidence와 지금 변경 목록이 다른지 봅니다."""

        if not baseline:
            return []
        recorded = set(baseline.get("changed_files") or ())
        if not recorded:
            return []
        current = set(changes.changed_files)
        added = sorted(current - recorded)
        removed = sorted(recorded - current)
        return [f"+{path}" for path in added] + [f"-{path}" for path in removed]

    def _implementation_baseline(self, run_id: str) -> dict[str, Any] | None:
        """PR #11이 남긴 implementation evidence를 읽습니다."""

        for row in self._store.events(limit=500):
            if row["kind"] != "implementation_completed" or row["run_id"] != run_id:
                continue
            try:
                detail = json.loads(row["detail"])
            except (ValueError, TypeError):
                return None
            changes = detail.get("changes") or {}
            return {
                "changed_files": changes.get("changed_files"),
                "before": (changes.get("before") or {}),
                "after": (changes.get("after") or {}),
            }
        return None

    def _task_for(self, run) -> dict[str, Any]:
        row = self._store.task_by_fingerprint(run.fingerprint)
        if row is None:
            return {}
        try:
            return json.loads(row["task_json"])
        except (ValueError, TypeError):
            return {}

    # -- subprocess step -------------------------------------------------

    def _command_step(
        self,
        validation_id: str,
        run,
        worker_id: str,
        step: ValidationStep,
        position: int,
        max_output_bytes: int,
    ) -> ValidationStepResult:
        """검증 명령을 별도 process로 실행합니다.

        환경은 OS 기본 allowlist만 씁니다. executor에 주었던 credential 환경은
        넘기지 않습니다. 검증은 provider 인증이 필요 없습니다.
        """

        request = ExecutorRequest(
            run_id=run.run_id,
            task_id=run.task_id,
            cwd=run.worktree_path,
            argv=step.argv,
            timeout_seconds=step.timeout_seconds,
            # 상속하지 않습니다. base_environment()가 allowlist를 적용합니다.
            environment={},
            max_output_bytes=max_output_bytes,
        )
        log_dir = self.log_dir(run.run_id, validation_id) / f"{position:02d}-{step.name}"
        started = utcnow()

        # 시작을 먼저 기록합니다. 중간에 죽으면 이 기록이 유일한 근거입니다.
        self._store.record_validation_step(
            validation_id,
            run.run_id,
            position=position,
            name=step.name,
            kind=step.kind.value,
            required=step.required,
            status=StepStatus.RUNNING.value,
            command=redact_argv(step.argv),
            evidence=step.evidence,
            started_at=to_iso(started),
        )

        try:
            handle = self._runtime.spawn(request, log_dir)
        except ExecutorError as error:
            return self._record_result(
                validation_id,
                run.run_id,
                position,
                ValidationStepResult(
                    name=step.name,
                    kind=step.kind,
                    required=step.required,
                    status=StepStatus.ERROR,
                    argv=step.argv,
                    started_at=to_iso(started),
                    finished_at=to_iso(utcnow()),
                    reason=f"command_missing:{error.category}",
                    evidence={**step.evidence, "detail": redact_line(error.message)},
                ),
            )

        self._store.record_validation_step(
            validation_id,
            run.run_id,
            position=position,
            name=step.name,
            kind=step.kind.value,
            required=step.required,
            status=StepStatus.RUNNING.value,
            command=redact_argv(step.argv),
            evidence=step.evidence,
            process_id=handle.pid,
            process_identity=handle.identity.to_dict(),
            process_started_at=handle.started_at,
            started_at=to_iso(started),
        )

        began = time.monotonic()
        result = self._runtime.wait(handle, request)
        duration = time.monotonic() - began

        status, reason = _classify(result)
        return self._record_result(
            validation_id,
            run.run_id,
            position,
            ValidationStepResult(
                name=step.name,
                kind=step.kind,
                required=step.required,
                status=status,
                argv=step.argv,
                exit_code=result.exit_code,
                started_at=result.started_at,
                finished_at=result.finished_at,
                duration_seconds=round(duration, 3),
                stdout=result.stdout.to_dict() if result.stdout else None,
                stderr=result.stderr.to_dict() if result.stderr else None,
                reason=reason,
                evidence={
                    **step.evidence,
                    "process_id": handle.pid,
                    "output_summary": _output_summary(result),
                },
            ),
            process_id=handle.pid,
            process_identity=handle.identity.to_dict(),
            process_started_at=handle.started_at,
        )

    # -- 기록 ------------------------------------------------------------

    def _record_result(
        self,
        validation_id: str,
        run_id: str,
        position: int,
        result: ValidationStepResult,
        **process: Any,
    ) -> ValidationStepResult:
        self._store.record_validation_step(
            validation_id,
            run_id,
            position=position,
            name=result.name,
            kind=result.kind.value,
            required=result.required,
            status=result.status.value,
            command=redact_argv(result.argv),
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            stdout=result.stdout,
            stderr=result.stderr,
            reason=result.reason,
            evidence=result.evidence,
            started_at=result.started_at,
            finished_at=result.finished_at or to_iso(utcnow()),
            **process,
        )
        return result

    def _persist(self, validation_id: str, report: ValidationReport) -> None:
        self._store.finish_validation(
            validation_id,
            status=ValidationStatus.FINISHED,
            outcome=report.outcome.value,
            summary=report.summary,
            warnings=report.warnings,
            failure_category=report.failure.value if report.failure else None,
        )

    def _apply_to_run(self, run_id: str, worker_id: str, report: ValidationReport) -> None:
        """검증 결과로 Run을 확정합니다.

        확정 직전에 승인과 claim을 다시 봅니다. 검증 도중 승인이 회수되면
        결과가 통과했더라도 `Succeeded`로 확정하지 않습니다.
        """

        run = self._store.run(run_id)
        if run is None or run.status.is_terminal:
            return

        if report.passed:
            gate = self.gate(run_id, worker_id)
            # 시작 시점에는 AwaitingValidation이었지만 지금은 Validating입니다.
            # 그 항목만 빼고 나머지 근거를 봅니다.
            lost = [
                name
                for name, ok in gate.checks.items()
                if not ok and name != "run_awaiting_validation"
            ]
            if lost:
                self._store.record_validation_event(
                    report.validation_id,
                    run_id,
                    "validation_authorization_lost",
                    {"failed_checks": lost},
                )
                self._store.finish_run(
                    run_id,
                    RunStatus.FAILED,
                    failure=RunFailure(
                        "policy_violation",
                        f"검증 도중 실행 근거를 잃었습니다: {', '.join(lost)}",
                    ),
                )
                return
            self._store.finish_run(run_id, RunStatus.SUCCEEDED)
            return

        failure = report.failure or ValidationFailure.PROCESS_FAILED
        self._store.finish_run(
            run_id,
            RunStatus.FAILED,
            failure=RunFailure(
                VALIDATION_TO_RUN_CATEGORY.get(failure, "unknown"),
                redact_line(report.summary) or failure.value,
            ),
        )

    @staticmethod
    def _summarize(
        results: list[ValidationStepResult],
        outcome: ValidationOutcome,
        warnings: tuple[str, ...],
    ) -> str:
        counts: dict[str, int] = {}
        for result in results:
            counts[result.status.value] = counts.get(result.status.value, 0) + 1
        parts = [f"{name}={value}" for name, value in sorted(counts.items())]
        blocking = [r.name for r in results if r.blocks_success]
        text = f"{outcome.value}: " + ", ".join(parts)
        if blocking:
            text += f" / 막은 step: {', '.join(blocking)}"
        if warnings:
            text += f" / 경고: {', '.join(warnings)}"
        return text

    def _heartbeat_loop(
        self, run_id: str, worker_id: str, stop: threading.Event, failures: list[str]
    ) -> None:
        """검증이 도는 동안 Run이 살아 있음을 알립니다.

        SQLite 연결은 스레드 간에 공유할 수 없으므로 자기 연결을 엽니다.
        """

        interval = max(self._config.heartbeat_interval_seconds, 1.0)
        store = None
        try:
            store = TaskStore(self._store.path)
            while not stop.wait(interval):
                try:
                    store.heartbeat(run_id, worker_id)
                except Exception as error:  # noqa: BLE001 - 분류만 남깁니다.
                    failures.append(type(error).__name__)
                    return
        except Exception as error:  # noqa: BLE001
            failures.append(type(error).__name__)
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    pass


def _classify(result) -> tuple[StepStatus, str]:
    """process 결과를 step 상태로 옮깁니다."""

    if result.failure is ExecutorFailure.TIMEOUT:
        return StepStatus.ERROR, "timeout"
    if result.failure is ExecutorFailure.CANCELLED:
        return StepStatus.ERROR, "cancelled"
    if result.failure is ExecutorFailure.SPAWN_FAILED:
        return StepStatus.ERROR, "command_missing:spawn_failed"
    if result.exit_code == 0:
        return StepStatus.PASSED, ""
    return StepStatus.FAILED, f"exit_code={result.exit_code}"


def _output_summary(result) -> str:
    """event에 넣을 짧은 요약. 전체 출력을 저장하지 않습니다."""

    text = ""
    if result.stderr:
        text = read_log_tail(result.stderr.path, max_bytes=4096)
    if not text.strip() and result.stdout:
        text = read_log_tail(result.stdout.path, max_bytes=4096)
    return redact_line(text, limit=OUTPUT_SUMMARY_CHARS)
