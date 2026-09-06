"""Atlas Task domain model.

정규 정의는 docs/specs/task-schema.md와 docs/specs/task-state-machine.md입니다.
이 모듈은 provider, transport, storage에 의존하지 않습니다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

SCHEMA_VERSION = "0.1"


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    DOCUMENTATION = "documentation"
    CODE = "code"
    DEPENDENCY = "dependency"
    CI_INFRASTRUCTURE = "ci_infrastructure"
    SECRETS_DEPLOYMENT = "secrets_deployment"


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TaskStatus(str, Enum):
    """docs/specs/task-state-machine.md의 상태. 이 슬라이스는 앞의 두 개만 생성합니다."""

    DRAFT = "Draft"
    NEEDS_CLARIFICATION = "NeedsClarification"
    PLANNED = "Planned"
    CONTEXT_READY = "ContextReady"
    QUEUED = "Queued"
    RUNNING = "Running"
    VALIDATING = "Validating"
    PULL_REQUEST_READY = "PullRequestReady"
    REVISION_REQUESTED = "RevisionRequested"
    APPROVED = "Approved"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


class Severity(str, Enum):
    """`ERROR`는 Task를 `NeedsClarification`으로 보내고 `ADVISORY`는 보내지 않습니다."""

    ERROR = "error"
    ADVISORY = "advisory"


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: Severity = Severity.ERROR
    field: str | None = None


@dataclass(frozen=True)
class Source:
    channel: str
    uri: str
    actor: str
    created_at: str


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    description: str
    # verification type은 Planner가 결정합니다. Intake에서는 유도하지 않습니다.
    verification: dict[str, Any] | None = None


@dataclass(frozen=True)
class ValidationCheck:
    id: str
    success: str
    type: str | None = None
    required: bool = True


@dataclass(frozen=True)
class ScopeSpec:
    """`allowed_scope` / `forbidden_scope`.

    Issue Form은 자유 서술이므로 분류하지 못한 항목은 버리지 않고
    `unclassified`에 보존합니다.
    """

    paths: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    external_systems: tuple[str, ...] = ()
    unclassified: tuple[str, ...] = ()

    def entries(self) -> tuple[str, ...]:
        return self.paths + self.operations + self.external_systems + self.unclassified


@dataclass(frozen=True)
class Task:
    """Intake가 생성하는 Task. 이후 단계가 채우는 필드는 `None`으로 둡니다."""

    task_id: str
    workspace_id: str
    project_id: str
    repository: str
    source: Source
    objective: str
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    allowed_scope: ScopeSpec
    forbidden_scope: ScopeSpec
    priority: Priority
    risk_level: RiskLevel
    validation_plan: tuple[ValidationCheck, ...]
    status: TaskStatus
    delivery: dict[str, Any]
    execution: dict[str, Any]
    audit: dict[str, Any]
    schema_version: str = SCHEMA_VERSION
    # Context Builder와 Planner의 산출물이므로 Intake에서는 비워 둡니다.
    context_refs: tuple[dict[str, Any], ...] = ()
    required_capabilities: tuple[str, ...] = ()
    preferred_role: str | None = None
    clarification_questions: tuple[dict[str, Any], ...] = ()
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IntakeResult:
    """Intake 결과.

    `status`가 `Draft`이면 `task`가 채워지고, `NeedsClarification`이면 `task`는
    `None`이며 `errors`가 사유를 담습니다.
    """

    status: TaskStatus
    # allowlist 밖 repository처럼 source를 관찰하기 전에 거부한 경우 `None`입니다.
    idempotency_fingerprint: str | None
    task: Task | None = None
    errors: tuple[ValidationIssue, ...] = ()
    advisories: tuple[ValidationIssue, ...] = ()
    deduplicated: bool = False

    @property
    def is_valid(self) -> bool:
        return self.status is TaskStatus.DRAFT and self.task is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "is_valid": self.is_valid,
            "deduplicated": self.deduplicated,
            "idempotency_fingerprint": self.idempotency_fingerprint,
            "task": self.task.to_dict() if self.task else None,
            "errors": [asdict(issue) for issue in self.errors],
            "advisories": [asdict(issue) for issue in self.advisories],
        }


class RunStatus(str, Enum):
    """Run 수명주기 상태.

    docs/specs/execution-runtime.md의 Run Boundary와
    docs/specs/task-state-machine.md를 따릅니다. Task 상태와 구분됩니다. Run이
    `SUCCEEDED`여도 Task는 사람 승인과 merge 전까지 `Completed`가 아닙니다.

    `ORPHANED`는 execution-runtime.md의 "process 상태를 증명할 수 없으면 새
    side effect를 허용하지 않고 recovery review로 기록한다"에 해당합니다.
    """

    PENDING = "Pending"
    RUNNING = "Running"
    # executor가 구현을 마쳤지만 아직 아무도 결과를 검증하지 않은 상태입니다.
    # terminal이 아니고, executor process도 heartbeat도 없습니다. 다음
    # validation slice가 여기서 시작합니다.
    AWAITING_VALIDATION = "AwaitingValidation"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    ORPHANED = "Orphaned"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_RUN_STATUSES

    @property
    def is_active(self) -> bool:
        """Task의 active Run 슬롯을 차지하는가.

        `AwaitingValidation`도 포함합니다. 아직 끝나지 않은 작업이므로 같은
        Task로 다른 Run이 시작되면 branch와 worktree가 충돌합니다.
        """

        return self in _ACTIVE_RUN_STATUSES

    @property
    def expects_heartbeat(self) -> bool:
        """worker가 heartbeat를 보내고 있어야 하는 상태인가.

        `AwaitingValidation`은 executor process가 이미 끝났으므로 heartbeat가
        오지 않습니다. staleness 판정 대상에서 빼야 정상 결과를 `Orphaned`로
        만들지 않습니다.
        """

        return self in _HEARTBEAT_RUN_STATUSES


_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.ORPHANED}
)
# active Run은 Task당 하나만 허용됩니다. store의 partial unique index와 같은 집합입니다.
_ACTIVE_RUN_STATUSES = frozenset(
    {RunStatus.PENDING, RunStatus.RUNNING, RunStatus.AWAITING_VALIDATION}
)

# heartbeat가 오고 있어야 하는 상태. staleness 판정은 이 집합만 봅니다.
# active와 분리해야 하는 이유는 `AwaitingValidation`이 "아직 끝나지 않았지만
# 돌고 있지도 않은" 상태이기 때문입니다.
_HEARTBEAT_RUN_STATUSES = frozenset({RunStatus.PENDING, RunStatus.RUNNING})

ACTIVE_RUN_STATUSES: tuple[str, ...] = tuple(
    sorted(status.value for status in _ACTIVE_RUN_STATUSES)
)

HEARTBEAT_RUN_STATUSES: tuple[str, ...] = tuple(
    sorted(status.value for status in _HEARTBEAT_RUN_STATUSES)
)

# docs/specs/task-state-machine.md의 Failure Taxonomy.
FAILURE_CATEGORIES: frozenset[str] = frozenset(
    {
        "clarification_required",
        "transient_executor",
        "authentication",
        "usage_exhausted",
        "validation_failed",
        "policy_violation",
        "project_boundary",
        "timeout",
        "cancelled_by_human",
        "worker_lost",
        "unknown",
    }
)


@dataclass(frozen=True)
class RunFailure:
    """구조화된 실패 사유.

    provider 응답 원문을 담지 않습니다. category는 Failure Taxonomy 어휘이고
    message는 redaction을 마친 사람이 읽을 설명입니다.
    """

    category: str
    message: str = ""

    def __post_init__(self) -> None:
        if self.category not in FAILURE_CATEGORIES:
            raise ValueError(
                f"알 수 없는 failure category: {self.category!r}. "
                f"허용값: {', '.join(sorted(FAILURE_CATEGORIES))}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category, "message": self.message}


@dataclass(frozen=True)
class Run:
    """한 Task의 실행 시도.

    worktree, branch, executor process, timeout은 이 슬라이스의 범위가 아니며
    해당 필드는 아직 만들지 않았습니다. execution-runtime.md의 Minimum Run
    Record 중 구현된 부분만 담습니다.
    """

    run_id: str
    task_id: str
    fingerprint: str
    claim_id: str
    worker_id: str
    status: RunStatus
    created_at: str
    heartbeat_at: str
    started_at: str | None = None
    finished_at: str | None = None
    failure_category: str | None = None
    failure_message: str | None = None
    previous_run_id: str | None = None
    # workspace(branch + worktree). 자세한 계약은 docs/specs/execution-runtime.md.
    workspace_status: "WorkspaceStatus" = None  # type: ignore[assignment]
    branch: str | None = None
    worktree_path: str | None = None
    base_branch: str | None = None
    base_revision: str | None = None
    workspace_created_at: str | None = None
    workspace_removed_at: str | None = None
    workspace_error: str | None = None

    def __post_init__(self) -> None:
        if self.workspace_status is None:
            object.__setattr__(self, "workspace_status", WorkspaceStatus.NONE)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["workspace_status"] = self.workspace_status.value
        return payload


class WorkspaceStatus(str, Enum):
    """Run workspace(branch + worktree)의 단계.

    git side effect를 DB transaction 안에서 잡지 않으려고 단계를 나눕니다.
    `PREPARING` 기록이 git 작업보다 먼저 남으므로, 중간에 실패해도 어떤 branch와
    경로를 정리해야 하는지 식별할 수 있습니다.
    """

    NONE = "none"
    PREPARING = "preparing"
    READY = "ready"
    FAILED = "failed"
    REMOVED = "removed"

    @property
    def has_resources(self) -> bool:
        """디스크에 branch나 worktree가 남아 있을 수 있는 단계인지."""

        return self in (WorkspaceStatus.PREPARING, WorkspaceStatus.READY, WorkspaceStatus.FAILED)
