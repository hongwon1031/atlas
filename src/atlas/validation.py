"""Intake validation.

docs/specs/task-schema.md의 "Intake에 필요한 필드", Risk Level, Scope Model,
Invariants를 검사합니다. 검증에 실패하면 Task는 생성되지 않고
docs/specs/task-state-machine.md의 `Draft -> NeedsClarification` 전이에 해당하는
결과를 돌려줍니다.
"""

from __future__ import annotations

from . import policy
from .idempotency import IdempotencyKey
from .issue_source import IssueRecord
from .parser import (
    ParsedBody,
    parse_checkboxes,
    split_checklist,
    split_items,
)
from .schema import (
    AcceptanceCriterion,
    IntakeResult,
    Priority,
    RiskLevel,
    ScopeSpec,
    Severity,
    Source,
    Task,
    TaskStatus,
    ValidationCheck,
    ValidationIssue,
)

REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("project", "Project"),
    ("objective", "Objective"),
    ("constraints", "Constraints"),
    ("acceptance_criteria", "Acceptance Criteria"),
    ("allowed_scope", "Allowed Scope"),
    ("forbidden_scope", "Forbidden Scope"),
    ("risk_level", "Risk Level"),
    ("priority", "Priority"),
    ("validation", "Validation"),
)


def _error(code: str, message: str, field: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, severity=Severity.ERROR, field=field)


def _advisory(code: str, message: str, field: str | None = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, severity=Severity.ADVISORY, field=field)


def classify_scope(entries: tuple[str, ...]) -> ScopeSpec:
    """자유 서술 scope 항목을 path, operation, external system으로 분류합니다.

    알려진 어휘에 맞지 않는 항목은 버리지 않고 `unclassified`에 남깁니다.
    """

    paths: list[str] = []
    operations: list[str] = []
    externals: list[str] = []
    unclassified: list[str] = []

    for entry in entries:
        token = entry.strip().casefold()
        if token in policy.SCOPE_OPERATIONS:
            operations.append(token)
        elif token in policy.EXTERNAL_SYSTEMS:
            externals.append(token)
        elif _looks_like_path(entry):
            paths.append(entry.strip())
        else:
            unclassified.append(entry.strip())

    return ScopeSpec(
        paths=tuple(paths),
        operations=tuple(operations),
        external_systems=tuple(externals),
        unclassified=tuple(unclassified),
    )


def _looks_like_path(entry: str) -> bool:
    value = entry.strip()
    if not value or " " in value:
        return False
    return "/" in value or "*" in value or "." in value


def build_acceptance_criteria(text: str) -> tuple[AcceptanceCriterion, ...]:
    return tuple(
        AcceptanceCriterion(id=f"AC-{index:02d}", description=description)
        for index, description in enumerate(split_checklist(text), start=1)
    )


def build_validation_plan(text: str) -> tuple[ValidationCheck, ...]:
    # `required=True`가 보수적 기본값입니다. Constitution은 비용을 이유로 검증을
    # 생략하지 않도록 요구합니다.
    return tuple(
        ValidationCheck(id=f"VAL-{index:02d}", success=success)
        for index, success in enumerate(split_items(text), start=1)
    )


def _validate_safety_confirmations(text: str) -> list[ValidationIssue]:
    """필수 confirmation 세 개가 모두 존재하고 체크됐는지 확인합니다.

    체크박스 개수만 세면 임의의 문구로 바꿔치기할 수 있으므로 문구를 대조합니다.
    """

    found: dict[str, list[bool]] = {}
    for box in parse_checkboxes(text):
        normalized = policy.normalize_confirmation(box.label)
        found.setdefault(normalized, []).append(box.checked)

    issues: list[ValidationIssue] = []
    for confirmation_id, label in policy.REQUIRED_SAFETY_CONFIRMATIONS:
        matches = found.get(policy.normalize_confirmation(label), [])
        if not matches:
            issues.append(
                _error(
                    "missing_safety_confirmation",
                    f"필수 Safety Confirmation이 없습니다 ({confirmation_id}): {label}",
                    field="safety_confirmations",
                )
            )
        elif len(matches) != 1:
            issues.append(
                _error(
                    "duplicate_safety_confirmation",
                    f"필수 Safety Confirmation이 중복됐습니다 ({confirmation_id}): {label}",
                    field="safety_confirmations",
                )
            )
        elif not matches[0]:
            issues.append(
                _error(
                    "safety_confirmation_unchecked",
                    f"확인하지 않은 Safety Confirmation이 있습니다 ({confirmation_id}): {label}",
                    field="safety_confirmations",
                )
            )
    return issues


def validate_intake(issue: IssueRecord, parsed: ParsedBody, key: IdempotencyKey) -> IntakeResult:
    errors: list[ValidationIssue] = []
    advisories: list[ValidationIssue] = []

    repo_policy = policy.repository_policy(issue.repository)
    if repo_policy is None:
        errors.append(
            _error(
                "repository_not_allowed",
                f"`{issue.repository}`는 repository allowlist에 없습니다.",
                field="repository",
            )
        )

    if issue.is_pull_request:
        errors.append(_error("issue_is_pull_request", "Pull Request는 Task 후보가 아닙니다."))
    if issue.state and issue.state != "open":
        errors.append(
            _error("issue_not_open", f"Issue 상태가 `{issue.state}`입니다. open Issue만 처리합니다.")
        )
    if not issue.title.startswith(policy.ISSUE_TITLE_MARKER):
        errors.append(
            _error(
                "missing_task_form_marker",
                f"Issue 제목이 `{policy.ISSUE_TITLE_MARKER}`로 시작하지 않습니다.",
                field="title",
            )
        )

    for label in parsed.duplicate_labels:
        errors.append(
            _error(
                "duplicate_field",
                f"`{label}` 항목이 두 번 이상 있습니다. 값을 하나로 정리해 주세요.",
                field=label,
            )
        )
    for label in parsed.unknown_labels:
        advisories.append(
            _advisory(
                "unknown_section",
                f"인식하지 못한 제목 `### {label}`은 값의 일부로 보존했습니다.",
                field=label,
            )
        )

    for field, label in REQUIRED_FIELDS:
        if not parsed.text(field):
            errors.append(_error("missing_required_field", f"`{label}` 항목이 비어 있습니다.", field=field))

    risk_level = _parse_enum(parsed.text("risk_level"), RiskLevel)
    if parsed.text("risk_level") and risk_level is None:
        errors.append(
            _error(
                "invalid_risk_level",
                "Risk Level 값이 올바르지 않습니다. "
                f"허용값: {', '.join(item.value for item in RiskLevel)}",
                field="risk_level",
            )
        )

    priority = _parse_enum(parsed.text("priority"), Priority)
    if parsed.text("priority") and priority is None:
        errors.append(
            _error(
                "invalid_priority",
                f"Priority 값이 올바르지 않습니다. 허용값: {', '.join(item.value for item in Priority)}",
                field="priority",
            )
        )

    project_id = parsed.text("project").strip()
    if repo_policy and project_id and project_id not in repo_policy.project_ids:
        errors.append(
            _error(
                "project_not_allowed",
                f"`{project_id}`는 `{issue.repository}`에 허용된 Project가 아닙니다.",
                field="project",
            )
        )

    errors.extend(_validate_safety_confirmations(parsed.text("safety_confirmations")))

    allowed_scope = classify_scope(split_items(parsed.text("allowed_scope")))
    forbidden_scope = classify_scope(split_items(parsed.text("forbidden_scope")))

    overlap = sorted(set(allowed_scope.entries()) & set(forbidden_scope.entries()))
    for entry in overlap:
        errors.append(
            _error(
                "scope_conflict",
                f"`{entry}`가 Allowed Scope와 Forbidden Scope에 모두 있습니다.",
                field="allowed_scope",
            )
        )

    acceptance_criteria = build_acceptance_criteria(parsed.text("acceptance_criteria"))
    validation_plan = build_validation_plan(parsed.text("validation"))

    if errors:
        return IntakeResult(
            status=TaskStatus.NEEDS_CLARIFICATION,
            idempotency_fingerprint=key.fingerprint(),
            errors=tuple(errors),
            advisories=tuple(advisories),
        )

    assert repo_policy is not None and risk_level is not None and priority is not None

    if not allowed_scope.operations:
        advisories.append(
            _advisory(
                "allowed_operations_missing",
                "Allowed Scope에 operation이 없습니다. 기본 deny 정책상 허용된 작업이 없는 것으로 해석됩니다.",
                field="allowed_scope",
            )
        )
    if allowed_scope.unclassified or forbidden_scope.unclassified:
        advisories.append(
            _advisory(
                "scope_unclassified_entries",
                "path나 operation으로 분류하지 못한 scope 항목이 있습니다. 사람이 확인해야 합니다.",
                field="allowed_scope",
            )
        )
    if risk_level is RiskLevel.SECRETS_DEPLOYMENT:
        advisories.append(
            _advisory(
                "secrets_deployment_not_auto_dispatchable",
                "`secrets_deployment` Task는 MVP에서 자동으로 Queued 또는 Running으로 전이할 수 없습니다.",
                field="risk_level",
            )
        )
    advisories.append(
        _advisory(
            "verification_unspecified",
            "Acceptance Criteria의 verification type은 Planner가 결정합니다. Intake에서는 비워 둡니다.",
            field="acceptance_criteria",
        )
    )

    task = Task(
        task_id=key.task_id,
        workspace_id=repo_policy.workspace_id,
        project_id=project_id,
        repository=issue.repository,
        source=Source(
            channel="github_issue",
            uri=issue.uri,
            actor=issue.author,
            created_at=issue.created_at,
        ),
        objective=parsed.text("objective"),
        constraints=split_items(parsed.text("constraints")),
        acceptance_criteria=acceptance_criteria,
        allowed_scope=allowed_scope,
        forbidden_scope=forbidden_scope,
        priority=priority,
        risk_level=risk_level,
        validation_plan=validation_plan,
        status=TaskStatus.DRAFT,
        delivery={
            "base_branch": repo_policy.default_base_branch,
            "pull_request_required": True,
            "human_merge_approval_required": True,
        },
        execution={
            "dispatch_mode": "manual",
            "primary_adapter": policy.PRIMARY_ADAPTER,
            "selected_adapter": None,
            "claim_id": None,
            "claimed_by": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "active_run_id": None,
        },
        audit={
            "created_by": issue.author,
            "created_at": issue.created_at,
            "updated_at": issue.updated_at,
            "correlation_id": f"issue-{issue.number}",
            "idempotency": key.to_dict(),
        },
        notes=parsed.text("notes"),
    )

    return IntakeResult(
        status=TaskStatus.DRAFT,
        idempotency_fingerprint=key.fingerprint(),
        task=task,
        advisories=tuple(advisories),
    )


def _parse_enum(raw: str, enum_type):
    value = raw.strip().casefold()
    for member in enum_type:
        if member.value == value:
            return member
    return None


__all__ = [
    "REQUIRED_FIELDS",
    "build_acceptance_criteria",
    "build_validation_plan",
    "classify_scope",
    "validate_intake",
]
