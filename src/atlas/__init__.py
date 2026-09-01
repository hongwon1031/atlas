"""Atlas Issue intake vertical slice.

이 package는 GitHub Issue 하나를 Atlas Task 후보로 parse하고 검증하는 데까지만
동작합니다. polling, claim, persistence, worktree, executor invocation,
PR delivery는 구현하지 않았습니다.
"""

from .idempotency import IdempotencyKey, InProcessIntakeCache
from .intake import IssueIntake
from .issue_source import (
    GitHubRestIssueSource,
    IssueRecord,
    IssueSource,
    IssueSourceError,
)
from .parser import ParsedBody, parse_issue_body
from .schema import (
    IntakeResult,
    Priority,
    RiskLevel,
    Task,
    TaskStatus,
    ValidationIssue,
)
from .validation import validate_intake

__all__ = [
    "GitHubRestIssueSource",
    "IdempotencyKey",
    "InProcessIntakeCache",
    "IntakeResult",
    "IssueIntake",
    "IssueRecord",
    "IssueSource",
    "IssueSourceError",
    "ParsedBody",
    "Priority",
    "RiskLevel",
    "Task",
    "TaskStatus",
    "ValidationIssue",
    "parse_issue_body",
    "validate_intake",
]
