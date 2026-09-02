"""Issue intake orchestration.

fetch -> parse -> validate 경로를 조립합니다. claim, lease, persistence,
executor invocation, PR delivery는 이 슬라이스의 범위가 아닙니다.
"""

from __future__ import annotations

import dataclasses

from . import policy
from .idempotency import (
    MANUAL_INTAKE_SIGNAL,
    IdempotencyKey,
    InProcessIntakeCache,
    compute_issue_revision,
    derive_task_id,
)
from .issue_source import IssueRecord, IssueSource
from .parser import parse_issue_body
from .schema import IntakeResult, Severity, TaskStatus, ValidationIssue
from .validation import validate_intake

DEFAULT_REPOSITORY = "hongwon1031/atlas"


def build_idempotency_key(issue: IssueRecord) -> IdempotencyKey:
    revision = compute_issue_revision(issue.title, issue.body)
    return IdempotencyKey(
        repository_id=issue.repository_id,
        issue_id=issue.issue_id,
        issue_revision=revision,
        signal_type=MANUAL_INTAKE_SIGNAL,
        signal_id=f"issue-{issue.number}@{revision[:12]}",
        task_id=derive_task_id(issue.number),
    )


class IssueIntake:
    """Issue 하나를 Task 후보로 변환합니다."""

    def __init__(self, source: IssueSource, cache: InProcessIntakeCache | None = None) -> None:
        self._source = source
        self._cache = cache if cache is not None else InProcessIntakeCache()

    def intake(self, issue_number: int, repository: str = DEFAULT_REPOSITORY) -> IntakeResult:
        # allowlist 검사는 네트워크 호출보다 먼저 수행합니다. worker token이
        # 접근할 수 있는 다른 repository를 경계 확인 전에 읽지 않기 위한 것입니다.
        if policy.repository_policy(repository) is None:
            return _repository_rejected(repository)

        return self.intake_record(self._source.fetch_issue(repository, issue_number))

    def intake_record(self, issue: IssueRecord) -> IntakeResult:
        """이미 조회한 Issue를 검증합니다. polling이 body를 재조회하지 않도록 분리했습니다."""

        if policy.repository_policy(issue.repository) is None:
            return _repository_rejected(issue.repository)

        key = build_idempotency_key(issue)
        # state와 is_pull_request는 내용 hash에 포함되지 않는 validation 입력이므로
        # cache 식별자에 함께 넣어 stale 결과를 돌려주지 않습니다.
        guard = (issue.state, issue.is_pull_request)

        cached = self._cache.get(key, guard)
        if cached is not None:
            return dataclasses.replace(cached, deduplicated=True)

        result = validate_intake(issue, parse_issue_body(issue.body), key)
        self._cache.put(key, result, guard)
        return result


def _repository_rejected(repository: str) -> IntakeResult:
    """GitHub 요청 없이 거부합니다. source를 관찰하지 않았으므로 fingerprint가 없습니다."""

    return IntakeResult(
        status=TaskStatus.NEEDS_CLARIFICATION,
        idempotency_fingerprint=None,
        errors=(
            ValidationIssue(
                code="repository_not_allowed",
                message=(
                    f"`{repository}`는 repository allowlist에 없습니다. "
                    "GitHub 요청을 보내지 않았습니다."
                ),
                severity=Severity.ERROR,
                field="repository",
            ),
        ),
    )
