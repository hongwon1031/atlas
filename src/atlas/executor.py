"""Provider-neutral executor contract.

Claude Code, Codex, local mock을 같은 계약으로 다루기 위한 경계입니다.
provider별 옵션(model, prompt 형식, credential 주입 방식)을 이 계약에 넣지
않습니다. adapter 내부에 격리합니다.

docs/specs/execution-runtime.md의 Run Boundary를 따릅니다.

- cwd는 반드시 해당 Run의 검증된 worktree입니다.
- 환경은 상속하지 않고 allowlist로 구성합니다.
- 모든 실행에 timeout이 있습니다.
- 종료 시 child process까지 정리합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .process_identity import ProcessIdentity


class ExecutionStatus(str, Enum):
    """executor process의 단계.

    Run status와 다릅니다. Run은 Task의 실행 시도이고, execution은 그 Run이
    띄운 OS process 하나의 수명주기입니다.
    """

    NONE = "none"
    # DB에 launch 의도를 먼저 기록한 상태. process는 아직 없습니다.
    STARTING = "Starting"
    RUNNING = "Running"
    CANCELLING = "Cancelling"
    FINISHED = "Finished"
    # spawn이나 attach 도중 실패했습니다. 남은 process가 있을 수 있어
    # reconciliation 대상입니다.
    FAILED = "Failed"

    @property
    def is_terminal(self) -> bool:
        return self in (ExecutionStatus.FINISHED, ExecutionStatus.FAILED)

    @property
    def is_active(self) -> bool:
        return self in (
            ExecutionStatus.STARTING,
            ExecutionStatus.RUNNING,
            ExecutionStatus.CANCELLING,
        )


ACTIVE_EXECUTION_STATUSES: tuple[str, ...] = tuple(
    sorted(status.value for status in ExecutionStatus if status.is_active)
)


class CancellationState(str, Enum):
    NONE = "none"
    REQUESTED = "requested"
    # graceful 종료를 요청했고 grace period를 기다리는 중입니다.
    TERMINATING = "terminating"
    FORCED = "forced"
    COMPLETED = "completed"
    # 종료를 시도했지만 process가 정말 끝났는지 증명하지 못했습니다.
    # identity를 확인할 수 없거나 다른 process일 수 있는 경우입니다.
    UNCONFIRMED = "unconfirmed"


class TerminationOutcome(str, Enum):
    """종료 시도의 결과.

    "종료를 요청했다"와 "종료를 확인했다"는 다릅니다. 확인하지 못한 상태를
    정상 종료로 확정하면 살아 있는 process를 놓칩니다.
    """

    # 종료가 필요하지 않았습니다. process가 스스로 끝났습니다.
    NOT_REQUIRED = "not_required"
    # 종료를 요청했고 process가 사라진 것을 확인했습니다.
    CONFIRMED = "confirmed"
    # 종료를 시도했지만 사라졌다고 증명하지 못했습니다. reconciliation 대상입니다.
    UNVERIFIED = "unverified"

    @property
    def process_may_be_alive(self) -> bool:
        return self is TerminationOutcome.UNVERIFIED


class ExecutorFailure(str, Enum):
    """executor 실패 분류.

    docs/specs/task-state-machine.md의 Failure Taxonomy로 옮길 수 있는
    어휘만 씁니다.
    """

    NONZERO_EXIT = "nonzero_exit"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    SPAWN_FAILED = "spawn_failed"
    SAFETY_GATE = "safety_gate"
    UNKNOWN = "unknown"


# executor가 실패했을 때 Run에 기록할 failure category.
FAILURE_TO_RUN_CATEGORY: dict[ExecutorFailure, str] = {
    ExecutorFailure.NONZERO_EXIT: "transient_executor",
    ExecutorFailure.TIMEOUT: "timeout",
    ExecutorFailure.CANCELLED: "cancelled_by_human",
    ExecutorFailure.SPAWN_FAILED: "transient_executor",
    ExecutorFailure.SAFETY_GATE: "policy_violation",
    ExecutorFailure.UNKNOWN: "unknown",
}


class ExecutorError(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass(frozen=True)
class ExecutorRequest:
    """한 번의 executor 실행 요청.

    provider 세부사항을 담지 않습니다. adapter가 자신의 launch spec으로
    번역합니다.
    """

    run_id: str
    task_id: str
    # 반드시 해당 Run의 검증된 worktree입니다. 호출자가 미리 검증합니다.
    cwd: str
    argv: tuple[str, ...]
    timeout_seconds: float
    # 상속하지 않고 여기 있는 값만 process 환경에 넣습니다.
    environment: dict[str, str] = field(default_factory=dict)
    # 출력에서 반드시 지워야 하는 raw 값. credential 주입 자체는 아직
    # 범위가 아니지만 redaction boundary는 지금부터 갖춥니다.
    secret_values: tuple[str, ...] = ()
    max_output_bytes: int = 1_048_576
    grace_period_seconds: float = 5.0
    # process stdin으로 흘려보낼 텍스트. argv에 넣으면 길이 제한과 shell
    # metacharacter 해석에 노출되므로, 임의 길이의 사용자 유래 텍스트는
    # 반드시 이 경로로 전달합니다. 비어 있으면 stdin은 닫힌 채 시작합니다.
    stdin_data: str = ""

    def to_dict(self) -> dict[str, Any]:
        from .redaction import redact_argv

        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "cwd": self.cwd,
            "argv": redact_argv(self.argv, self.secret_values),
            "timeout_seconds": self.timeout_seconds,
            "environment_keys": sorted(self.environment),
            "max_output_bytes": self.max_output_bytes,
            # 내용은 남기지 않습니다. prompt에는 Issue 본문이 들어갑니다.
            "stdin_bytes": len(self.stdin_data.encode("utf-8")),
        }


@dataclass(frozen=True)
class OutputCapture:
    """stdout 또는 stderr 한 쪽의 수집 결과.

    내용 자체를 들고 있지 않습니다. 경로와 metadata만 남겨 메모리와 event를
    보호합니다.
    """

    path: str
    bytes_written: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "bytes_written": self.bytes_written,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class ProcessHandle:
    """spawn된 process를 식별하는 값.

    PID만으로는 PID 재사용을 구분할 수 없으므로 identity를 함께 들고 있습니다.
    """

    pid: int
    identity: ProcessIdentity
    started_at: str
    # POSIX에서 process group을 만들었으면 그 id. Windows는 None입니다.
    process_group_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "identity": self.identity.to_dict(),
            "started_at": self.started_at,
            "process_group_id": self.process_group_id,
        }


@dataclass(frozen=True)
class ExecutorResult:
    """실행 결과."""

    run_id: str
    executor_name: str
    status: ExecutionStatus
    exit_code: int | None
    started_at: str
    finished_at: str | None
    stdout: OutputCapture | None = None
    stderr: OutputCapture | None = None
    failure: ExecutorFailure | None = None
    detail: str = ""
    cancellation_state: CancellationState = CancellationState.NONE
    termination: TerminationOutcome = TerminationOutcome.NOT_REQUIRED
    termination_evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionStatus.FINISHED and self.exit_code == 0

    @property
    def process_may_be_alive(self) -> bool:
        """process가 아직 살아 있을 수 있으면 terminal로 확정하면 안 됩니다."""

        return self.termination.process_may_be_alive

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "executor_name": self.executor_name,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stdout": self.stdout.to_dict() if self.stdout else None,
            "stderr": self.stderr.to_dict() if self.stderr else None,
            "failure": self.failure.value if self.failure else None,
            "detail": self.detail,
            "cancellation_state": self.cancellation_state.value,
            "termination": self.termination.value,
            "termination_evidence": self.termination_evidence,
            "succeeded": self.succeeded,
            "process_may_be_alive": self.process_may_be_alive,
        }


class ExecutorAdapter(Protocol):
    """provider를 교체할 수 있는 실행 경계."""

    @property
    def name(self) -> str:
        """`mock_local`, `claude_code_self_hosted`처럼 registry에서 쓰는 이름."""
        ...

    @property
    def provider(self) -> str:
        """`local`, `anthropic`처럼 provider identity."""
        ...

    def spawn(self, request: ExecutorRequest, log_dir: Path) -> ProcessHandle:
        """process를 시작하고 handle을 돌려줍니다. 완료를 기다리지 않습니다."""
        ...

    def wait(
        self, handle: ProcessHandle, request: ExecutorRequest
    ) -> ExecutorResult:
        """완료를 기다립니다. timeout이면 종료시키고 실패로 보고합니다."""
        ...

    def cancel(
        self, handle: ProcessHandle, grace_period_seconds: float
    ) -> CancellationState:
        """graceful 종료 후 grace period가 지나면 강제 종료합니다."""
        ...
