"""Atlas Issue intake vertical slice.

이 package는 GitHub Issue 하나를 Atlas Task 후보로 parse하고 검증하는 데까지만
동작합니다. polling, claim, persistence, worktree, executor invocation,
PR delivery는 구현하지 않았습니다.
"""

from .config import ClaimConfig, PollingConfig, RunConfig, WorkerConfig, WorkspaceConfig
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
from .gitcmd import GitError, GitRunner
from .polling import IssuePoller, PollReport, candidate_rejection, is_task_candidate
from .reconciliation import ReconcileReport, RunReconciler, RunVerdict
from .schema import (
    IntakeResult,
    Priority,
    RiskLevel,
    Run,
    RunFailure,
    RunStatus,
    Task,
    TaskStatus,
    ValidationIssue,
    WorkspaceStatus,
)
from .store import Claim, Registration, RunError, TaskStore, WorkspaceConflict
from .workspace import WorkspaceError, WorkspacePlanner, branch_name
from .workspace_service import CleanupResult, WorkspaceResult, WorkspaceService
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
    "ReconcileReport",
    "Registration",
    "RiskLevel",
    "Run",
    "RunConfig",
    "RunError",
    "RunFailure",
    "RunReconciler",
    "RunStatus",
    "RunVerdict",
    "Task",
    "TaskStatus",
    "TaskStore",
    "CleanupResult",
    "GitError",
    "GitRunner",
    "ValidationIssue",
    "WorkerConfig",
    "WorkspaceConfig",
    "WorkspaceConflict",
    "WorkspaceError",
    "WorkspacePlanner",
    "WorkspaceResult",
    "WorkspaceService",
    "WorkspaceStatus",
    "branch_name",
    "candidate_rejection",
    "is_task_candidate",
    "parse_issue_body",
    "validate_intake",
]
