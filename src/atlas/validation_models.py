"""Validation의 provider-neutral 계약.

executor가 "무엇을 바꿨는가"를 다룬다면 validation은 "그 변경이 통과하는가"를
다룹니다. 두 단계는 다른 실패 어휘를 가집니다.

이 모듈에는 provider 이름이 들어가지 않습니다. Claude가 만든 변경이든 사람이
만든 변경이든 같은 방식으로 검증합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ValidationStatus(str, Enum):
    """validation attempt 하나의 수명주기.

    Run status와 다릅니다. Run은 Task의 실행 시도이고, validation은 그 Run의
    결과를 검증하는 한 번의 시도입니다.
    """

    # DB에 의도를 먼저 기록한 상태. 아직 step을 시작하지 않았습니다.
    STARTING = "Starting"
    RUNNING = "Running"
    FINISHED = "Finished"
    # 시작하거나 진행하다 실패했습니다. 남은 process가 있을 수 있어
    # reconciliation 대상입니다.
    FAILED = "Failed"

    @property
    def is_terminal(self) -> bool:
        return self in (ValidationStatus.FINISHED, ValidationStatus.FAILED)

    @property
    def is_active(self) -> bool:
        return self in (ValidationStatus.STARTING, ValidationStatus.RUNNING)


ACTIVE_VALIDATION_STATUSES: tuple[str, ...] = tuple(
    sorted(status.value for status in ValidationStatus if status.is_active)
)


class StepKind(str, Enum):
    """검증 step의 종류.

    repository마다 갖춘 것이 다릅니다. 모든 repository가 lint나 typecheck를
    가진다고 가정하지 않습니다.
    """

    WORKSPACE_INTEGRITY = "workspace_integrity"
    GIT_POLICY = "git_policy"
    TESTS = "tests"
    COMPILE = "compile"
    LINT = "lint"
    TYPECHECK = "typecheck"
    BUILD = "build"


class StepStatus(str, Enum):
    """step 하나의 결과.

    `skipped`와 `passed`를 구분합니다. "검사했고 통과했다"와 "검사할 근거가
    없었다"는 전혀 다른 사실이고, 뭉뚱그리면 검증되지 않은 변경을 통과시킵니다.
    """

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    # 실행할 근거가 없어 건너뛰었습니다. 실패가 아닙니다.
    SKIPPED = "skipped"
    # 실행하려 했지만 수행하지 못했습니다. 통과로 볼 수 없습니다.
    ERROR = "error"

    @property
    def blocks_success(self) -> bool:
        return self in (StepStatus.FAILED, StepStatus.ERROR)


class ValidationOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    # 결과를 판단할 수 없습니다. 자동으로 성공 처리하지 않습니다.
    AMBIGUOUS = "ambiguous"


class ValidationFailure(str, Enum):
    """validation 실패 분류.

    provider 어휘를 쓰지 않습니다. 어떤 executor가 만든 변경이든 같은 분류를
    씁니다.
    """

    POLICY_VIOLATION = "validation_policy_violation"
    WORKSPACE_INVALID = "validation_workspace_invalid"
    TEST_FAILED = "validation_test_failed"
    LINT_FAILED = "validation_lint_failed"
    TYPECHECK_FAILED = "validation_typecheck_failed"
    COMPILE_FAILED = "validation_compile_failed"
    BUILD_FAILED = "validation_build_failed"
    COMMAND_MISSING = "validation_command_missing"
    TIMEOUT = "validation_timeout"
    PROCESS_FAILED = "validation_process_failed"
    # 결과를 저장하기 전에 중단돼 상태를 확정할 수 없습니다.
    STATE_AMBIGUOUS = "validation_state_ambiguous"
    GATE_FAILED = "validation_gate_failed"


# docs/specs/task-state-machine.md의 Failure Taxonomy 어휘로만 옮깁니다.
VALIDATION_TO_RUN_CATEGORY: dict[ValidationFailure, str] = {
    ValidationFailure.POLICY_VIOLATION: "policy_violation",
    ValidationFailure.WORKSPACE_INVALID: "policy_violation",
    ValidationFailure.TEST_FAILED: "validation_failed",
    ValidationFailure.LINT_FAILED: "validation_failed",
    ValidationFailure.TYPECHECK_FAILED: "validation_failed",
    ValidationFailure.COMPILE_FAILED: "validation_failed",
    ValidationFailure.BUILD_FAILED: "validation_failed",
    ValidationFailure.COMMAND_MISSING: "validation_failed",
    ValidationFailure.TIMEOUT: "timeout",
    ValidationFailure.PROCESS_FAILED: "transient_executor",
    ValidationFailure.STATE_AMBIGUOUS: "unknown",
    ValidationFailure.GATE_FAILED: "policy_violation",
}

# step 종류별로 실패했을 때 쓸 분류.
KIND_TO_FAILURE: dict[StepKind, ValidationFailure] = {
    StepKind.WORKSPACE_INTEGRITY: ValidationFailure.WORKSPACE_INVALID,
    StepKind.GIT_POLICY: ValidationFailure.POLICY_VIOLATION,
    StepKind.TESTS: ValidationFailure.TEST_FAILED,
    StepKind.COMPILE: ValidationFailure.COMPILE_FAILED,
    StepKind.LINT: ValidationFailure.LINT_FAILED,
    StepKind.TYPECHECK: ValidationFailure.TYPECHECK_FAILED,
    StepKind.BUILD: ValidationFailure.BUILD_FAILED,
}


@dataclass(frozen=True)
class ValidationStep:
    """실행할 검증 하나의 계획.

    `argv`가 비어 있으면 subprocess 없이 Atlas가 직접 수행하는 내부 step입니다
    (workspace integrity, git policy).
    """

    name: str
    kind: StepKind
    required: bool
    # 왜 이 step을 선택했는지. 추측이 아니라 발견한 파일과 근거를 남깁니다.
    evidence: dict[str, Any] = field(default_factory=dict)
    argv: tuple[str, ...] = ()
    timeout_seconds: float = 600.0
    # 계획 단계에서 이미 건너뛰기로 정해진 step. 이유를 함께 남깁니다.
    skip_reason: str = ""
    # 계획 단계에서 이미 수행 불가로 정해진 step.
    error_reason: str = ""

    @property
    def is_internal(self) -> bool:
        return not self.argv

    def to_dict(self) -> dict[str, Any]:
        from .redaction import redact_argv

        return {
            "name": self.name,
            "kind": self.kind.value,
            "required": self.required,
            "argv": redact_argv(self.argv),
            "timeout_seconds": self.timeout_seconds,
            "evidence": self.evidence,
            "skip_reason": self.skip_reason,
            "error_reason": self.error_reason,
        }


@dataclass(frozen=True)
class ValidationPlan:
    """이 repository에서 무엇을 어떻게 검증할지.

    추측으로 명령을 만들지 않습니다. 모든 step은 repository에서 발견한 근거를
    가집니다.
    """

    steps: tuple[ValidationStep, ...]
    ecosystem: str = "unknown"
    # 검증 근거로 발견한 파일과 설정.
    discovery: dict[str, Any] = field(default_factory=dict)

    @property
    def required_steps(self) -> tuple[ValidationStep, ...]:
        return tuple(step for step in self.steps if step.required)

    @property
    def has_test_capability(self) -> bool:
        return any(
            step.kind is StepKind.TESTS and not step.skip_reason for step in self.steps
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ecosystem": self.ecosystem,
            "discovery": self.discovery,
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(frozen=True)
class ValidationStepResult:
    """step 하나의 실행 결과."""

    name: str
    kind: StepKind
    required: bool
    status: StepStatus
    argv: tuple[str, ...] = ()
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    stdout: dict[str, Any] | None = None
    stderr: dict[str, Any] | None = None
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def blocks_success(self) -> bool:
        return self.required and self.status.blocks_success

    def to_dict(self) -> dict[str, Any]:
        from .redaction import redact_argv

        return {
            "name": self.name,
            "kind": self.kind.value,
            "required": self.required,
            "status": self.status.value,
            "argv": redact_argv(self.argv),
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ValidationReport:
    """validation attempt 하나의 최종 결과."""

    validation_id: str
    run_id: str
    outcome: ValidationOutcome
    results: tuple[ValidationStepResult, ...]
    plan: ValidationPlan
    failure: ValidationFailure | None = None
    summary: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.outcome is ValidationOutcome.PASSED

    @property
    def blocking(self) -> tuple[ValidationStepResult, ...]:
        return tuple(result for result in self.results if result.blocks_success)

    def to_dict(self) -> dict[str, Any]:
        return {
            "validation_id": self.validation_id,
            "run_id": self.run_id,
            "outcome": self.outcome.value,
            "failure": self.failure.value if self.failure else None,
            "summary": self.summary,
            "warnings": list(self.warnings),
            "plan": self.plan.to_dict(),
            "results": [result.to_dict() for result in self.results],
        }


def decide(
    results: tuple[ValidationStepResult, ...] | list[ValidationStepResult],
    plan: ValidationPlan,
) -> tuple[ValidationOutcome, ValidationFailure | None, tuple[str, ...]]:
    """step 결과를 모아 최종 판정을 냅니다.

    **"실행한 명령이 모두 exit 0"을 성공으로 정의하지 않습니다.** required step이
    하나라도 실패하거나 수행되지 못했으면 실패입니다. optional step의 실패는
    경고로 남기고 판정을 막지 않습니다.
    """

    results = tuple(results)
    warnings: list[str] = []

    blocking = [result for result in results if result.blocks_success]
    if blocking:
        first = blocking[0]
        failure = KIND_TO_FAILURE.get(first.kind, ValidationFailure.PROCESS_FAILED)
        if first.status is StepStatus.ERROR and first.reason.startswith("command_missing"):
            failure = ValidationFailure.COMMAND_MISSING
        if first.reason == "timeout":
            failure = ValidationFailure.TIMEOUT
        return ValidationOutcome.FAILED, failure, tuple(warnings)

    for result in results:
        if not result.required and result.status.blocks_success:
            warnings.append(f"optional step {result.name}이(가) {result.status.value}입니다.")

    if not plan.has_test_capability:
        # 테스트가 없는 repository입니다. 무조건 실패시키지 않되, 검증되지
        # 않았다는 사실을 반드시 남깁니다.
        warnings.append("no_tests_discovered")
        warnings.append("validation_passed_with_no_tests")

    return ValidationOutcome.PASSED, None, tuple(warnings)
