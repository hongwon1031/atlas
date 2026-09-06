"""구현 Run 하나를 조율합니다.

`ExecutionService`가 process 수명주기를 다루고, 이 모듈은 그 위에서
"Task가 실제로 구현됐는가"를 판단할 근거를 모읍니다.

핵심 구분입니다.

- **executor process success** — CLI가 정상 종료했는가.
- **task implementation result** — worktree가 실제로 바뀌었는가.

둘은 다릅니다. exit 0이어도 아무것도 바꾸지 않았을 수 있고, 범위 밖을
바꿨을 수도 있습니다. 이번 slice에는 validation pipeline이 없으므로
**탐지와 기록까지만** 하고 Run을 성공으로 확정하지 않습니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .claude_code import (
    CLAUDE_TO_EXECUTOR_FAILURE,
    CLAUDE_TO_RUN_CATEGORY,
    ClaudeCodeExecutor,
    ClaudeFailure,
    ClaudeOutcome,
)
from .claude_prompt import build_prompt
from .execution_service import ExecutionService
from .executor import ExecutorRequest, ExecutorResult
from .local_process import read_log_tail
from .redaction import redact_line
from .schema import RunFailure, RunStatus
from .store import TaskStore
from .worktree_changes import (
    ChangeReport,
    ImplementationOutcome,
    compare,
    safe_capture,
)

# stderr 요약으로 남길 길이. 원문은 log artifact에만 둡니다.
STDERR_SUMMARY_CHARS = 300


@dataclass(frozen=True)
class ImplementationResult:
    """한 번의 구현 시도 결과."""

    run_id: str
    execution_id: str
    process_succeeded: bool
    outcome: ImplementationOutcome
    changes: ChangeReport | None
    claude: ClaudeOutcome | None
    failure: ClaudeFailure | None
    result: ExecutorResult

    @property
    def implemented(self) -> bool:
        return self.outcome is ImplementationOutcome.CHANGES_APPLIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "execution_id": self.execution_id,
            "process_succeeded": self.process_succeeded,
            "implementation_outcome": self.outcome.value,
            "failure": self.failure.value if self.failure else None,
            "changes": self.changes.to_dict() if self.changes else None,
            "claude": self.claude.to_dict() if self.claude else None,
            "execution": self.result.to_dict(),
        }


class ImplementationRunner:
    """Claude Code로 Task를 구현하고 결과를 판정합니다."""

    def __init__(
        self,
        store: TaskStore,
        service: ExecutionService,
        adapter: ClaudeCodeExecutor,
        git_timeout_seconds: float = 30.0,
    ) -> None:
        self._store = store
        self._service = service
        self._adapter = adapter
        self._git_timeout = git_timeout_seconds

    def task_for(self, run_id: str) -> dict[str, Any]:
        """Run에 연결된 current Task revision을 DB에서 읽습니다.

        prompt 입력은 저장된 Task contract뿐입니다. 자유 prompt로 Task 경계를
        우회하는 경로를 만들지 않습니다.
        """

        run = self._store.run(run_id)
        if run is None:
            raise LookupError(f"Run을 찾을 수 없습니다: {run_id}")
        row = self._store.task_by_fingerprint(run.fingerprint)
        if row is None:
            raise LookupError(f"Run {run_id}의 Task revision을 찾을 수 없습니다.")
        import json

        return json.loads(row["task_json"])

    def build_prompt_for(self, run_id: str) -> str:
        run = self._store.run(run_id)
        task = self.task_for(run_id)
        return build_prompt(task, run_id, branch=run.branch if run else None)

    def run(
        self,
        run_id: str,
        worker_id: str,
        prompt_override: str | None = None,
        **kwargs: Any,
    ) -> ImplementationResult:
        """구현을 한 번 시도하고 결과를 기록합니다.

        `prompt_override`는 개발용입니다. 운영 경로에서는 쓰지 않습니다.
        """

        run = self._store.run(run_id)
        if run is None:
            raise LookupError(f"Run을 찾을 수 없습니다: {run_id}")
        if not run.worktree_path:
            raise LookupError(f"Run {run_id}에 worktree가 없습니다.")

        task = self.task_for(run_id)
        prompt = prompt_override or build_prompt(task, run_id, branch=run.branch)

        # 실행 전 상태를 먼저 남깁니다. 실행 후에만 찍으면 무엇이 이번 실행의
        # 결과인지 구분할 수 없습니다.
        before = safe_capture(run.worktree_path, self._git_timeout)

        environment = dict(kwargs.pop("environment", {}) or {})
        secrets = tuple(kwargs.get("secret_values") or ())
        base = ExecutorRequest(
            run_id=run_id,
            task_id=run.task_id,
            cwd=run.worktree_path,
            argv=(),
            timeout_seconds=float(kwargs.get("timeout_seconds") or 900.0),
            environment=environment,
            secret_values=secrets,
            max_output_bytes=int(kwargs.get("max_output_bytes") or 1_048_576),
        )
        request = self._adapter.build_request(base, prompt, environment=environment)

        outcome = self._service.run(
            run_id, worker_id, request.argv, stdin_data=prompt, environment=environment, **kwargs
        )
        result = outcome.result

        after = safe_capture(run.worktree_path, self._git_timeout)
        claude = self._adapter.interpret(result, request)
        stderr_tail = (
            read_log_tail(result.stderr.path, max_bytes=4096) if result.stderr else ""
        )
        failure = _classify(result, claude, stderr_tail)

        process_succeeded = result.succeeded and not claude.is_error
        if before is not None and after is not None:
            changes = compare(before, after, task, process_succeeded=process_succeeded)
        else:
            changes = None

        report = _decide(
            run_id=run_id,
            execution_id=outcome.execution_id,
            process_succeeded=process_succeeded,
            changes=changes,
            claude=claude,
            failure=failure,
            result=result,
        )
        self._record(report, stderr_tail)
        self._apply_to_run(report)
        return report

    # -- 기록과 Run 반영 --------------------------------------------------

    def _record(self, report: ImplementationResult, stderr_tail: str) -> None:
        """근거를 event로 남깁니다. Claude 응답 전문은 넣지 않습니다."""

        detail: dict[str, Any] = {
            "implementation_outcome": report.outcome.value,
            "process_succeeded": report.process_succeeded,
            "failure": report.failure.value if report.failure else None,
            "executor": self._adapter.name,
            "provider": self._adapter.provider,
        }
        if report.claude is not None:
            detail["claude"] = report.claude.to_dict()
        if report.changes is not None:
            detail["changes"] = report.changes.to_dict()
        if stderr_tail:
            detail["stderr_summary"] = redact_line(stderr_tail, limit=STDERR_SUMMARY_CHARS)

        self._store.record_execution_event(
            report.execution_id, report.run_id, "implementation_completed", detail
        )

    def _apply_to_run(self, report: ImplementationResult) -> None:
        """Run status에 반영합니다.

        구현이 성공했다고 Run을 `Succeeded`로 만들지 않습니다. 아직 validation
        pipeline이 없어서 "코드가 올바른지"를 아무도 확인하지 않았습니다.
        성공 경로에서는 Run을 `Running`으로 남기고 validation을 기다립니다.
        """

        run = self._store.run(report.run_id)
        if run is None or run.status.is_terminal:
            return

        if report.outcome is ImplementationOutcome.CHANGES_APPLIED:
            # 성공 경로. Run을 종료하지 않습니다.
            return

        if report.outcome is ImplementationOutcome.NO_CHANGES:
            self._store.finish_run(
                report.run_id,
                RunStatus.FAILED,
                failure=RunFailure(
                    CLAUDE_TO_RUN_CATEGORY[ClaudeFailure.NO_CHANGES],
                    "executor가 정상 종료했지만 worktree가 바뀌지 않았습니다.",
                ),
            )
            return

        if report.outcome is ImplementationOutcome.POLICY_VIOLATION:
            reasons = ", ".join(report.changes.violations) if report.changes else ""
            self._store.finish_run(
                report.run_id,
                RunStatus.FAILED,
                failure=RunFailure(
                    CLAUDE_TO_RUN_CATEGORY[ClaudeFailure.POLICY_VIOLATION],
                    f"허용 범위를 벗어난 변경입니다: {reasons}",
                ),
            )
            return

        # UNKNOWN. process가 실패했거나 판단 근거가 없습니다.
        failure = report.failure
        if failure is None:
            self._service.apply_to_run(report.run_id, report.result)
            return
        self._store.finish_run(
            report.run_id,
            RunStatus.FAILED,
            failure=RunFailure(
                CLAUDE_TO_RUN_CATEGORY.get(failure, "unknown"),
                redact_line(report.claude.result_text if report.claude else "")
                or failure.value,
            ),
        )


def _classify(
    result: ExecutorResult, claude: ClaudeOutcome, stderr_tail: str
) -> ClaudeFailure | None:
    from .claude_code import classify_failure

    return classify_failure(result, claude, stderr_tail)


def _decide(
    *,
    run_id: str,
    execution_id: str,
    process_succeeded: bool,
    changes: ChangeReport | None,
    claude: ClaudeOutcome,
    failure: ClaudeFailure | None,
    result: ExecutorResult,
) -> ImplementationResult:
    """process 결과와 변경 감지를 합쳐 최종 판정을 만듭니다."""

    if not process_succeeded:
        outcome = ImplementationOutcome.UNKNOWN
    elif changes is None:
        # git 상태를 읽지 못했습니다. 바뀌었다고 단정하지 않습니다.
        outcome = ImplementationOutcome.UNKNOWN
    else:
        outcome = changes.outcome

    if outcome is ImplementationOutcome.NO_CHANGES and failure is None:
        failure = ClaudeFailure.NO_CHANGES
    if outcome is ImplementationOutcome.POLICY_VIOLATION and failure is None:
        failure = ClaudeFailure.POLICY_VIOLATION

    return ImplementationResult(
        run_id=run_id,
        execution_id=execution_id,
        process_succeeded=process_succeeded,
        outcome=outcome,
        changes=changes,
        claude=claude,
        failure=failure,
        result=result,
    )
