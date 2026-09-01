"""Issue intake orchestration.

fetch -> parse -> validate 경로를 조립합니다. claim, lease, persistence,
executor invocation, PR delivery는 이 슬라이스의 범위가 아닙니다.
"""

from __future__ import annotations

import dataclasses

from .idempotency import (
    MANUAL_INTAKE_SIGNAL,
    IdempotencyKey,
    InProcessIntakeCache,
    compute_issue_revision,
    derive_task_id,
)
from .issue_source import IssueRecord, IssueSource
from .parser import parse_issue_body
from .schema import IntakeResult
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
        issue = self._source.fetch_issue(repository, issue_number)
        key = build_idempotency_key(issue)

        cached = self._cache.get(key)
        if cached is not None:
            return dataclasses.replace(cached, deduplicated=True)

        result = validate_intake(issue, parse_issue_body(issue.body), key)
        self._cache.put(key, result)
        return result
