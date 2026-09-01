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
