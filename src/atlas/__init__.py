"""Atlas Issue intake vertical slice.

이 package는 GitHub Issue 하나를 Atlas Task 후보로 parse하고 검증하는 데까지만
동작합니다. polling, claim, persistence, worktree, executor invocation,
PR delivery는 구현하지 않았습니다.
"""

from .config import ClaimConfig, PollingConfig, WorkerConfig
from .idempotency import IdempotencyKey, InProcessIntakeCache
from .intake import IssueIntake
from .issue_source import (
    GitHubRestIssueSource,
    IssueLister,
    IssueRecord,
    IssueSource,
    IssueSourceError,
)
from .parser import ParsedBody, parse_issue_body
from .polling import IssuePoller, PollReport, candidate_rejection, is_task_candidate
from .schema import (
    IntakeResult,
    Priority,
    RiskLevel,
    Task,
    TaskStatus,
    ValidationIssue,
)
from .store import Claim, Registration, TaskStore
from .validation import validate_intake

__all__ = [
    "Claim",
    "ClaimConfig",
    "GitHubRestIssueSource",
    "IdempotencyKey",
    "InProcessIntakeCache",
    "IntakeResult",
    "IssueIntake",
    "IssueLister",
    "IssuePoller",
    "IssueRecord",
    "IssueSource",
    "IssueSourceError",
    "ParsedBody",
    "PollReport",
    "PollingConfig",
    "Priority",
    "Registration",
    "RiskLevel",
    "Task",
    "TaskStatus",
    "TaskStore",
    "ValidationIssue",
    "WorkerConfig",
    "candidate_rejection",
    "is_task_candidate",
    "parse_issue_body",
    "validate_intake",
]
