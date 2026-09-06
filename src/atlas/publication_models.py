"""Git publication의 provider-neutral 계약.

validation이 "그 변경이 통과하는가"를 다뤘다면 publication은 "그 변경을 어떻게
전달하는가"를 다룹니다.

**Run이 `Succeeded`인 것과 publication이 성공한 것은 다른 사실입니다.** 코드가
검증됐다는 사실은 push가 실패해도 변하지 않습니다. 그래서 publication은 Run
status가 아니라 별도의 operational attempt로 모델링합니다.

외부 side effect를 다루므로 두 가지가 특히 중요합니다.

- **각 단계 뒤에 durable checkpoint**를 둡니다. commit은 성공했는데 DB 저장
  전에 죽으면, 재시작한 worker가 그 사실을 알아야 다시 commit하지 않습니다.
- **모호한 외부 상태를 자동으로 덮어쓰지 않습니다.** remote가 다른 commit을
  가리키면 force push가 아니라 recovery입니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PublicationStatus(str, Enum):
    """publication attempt 하나의 단계.

    단계를 잘게 나누는 이유는 crash window마다 어디까지 진행됐는지 알아야
    하기 때문입니다. "실패했다"만으로는 commit을 다시 만들어도 되는지,
    push를 다시 해도 되는지 판단할 수 없습니다.
    """

    # DB에 의도를 먼저 기록했습니다. 아직 아무 side effect도 없습니다.
    STARTING = "Starting"
    COMMITTING = "Committing"
    PUSHING = "Pushing"
    CREATING_PR = "CreatingPR"
    PUBLISHED = "Published"
    # 외부 상태가 모호하거나 충돌합니다. 자동으로 덮어쓰지 않습니다.
    RECOVERY_REQUIRED = "RecoveryRequired"
    FAILED = "Failed"

    @property
    def is_terminal(self) -> bool:
        return self in (PublicationStatus.PUBLISHED, PublicationStatus.FAILED)

    @property
    def is_active(self) -> bool:
        """reconciliation이 계속 봐야 하는 상태."""

        return self in (
            PublicationStatus.STARTING,
            PublicationStatus.COMMITTING,
            PublicationStatus.PUSHING,
            PublicationStatus.CREATING_PR,
            PublicationStatus.RECOVERY_REQUIRED,
        )


ACTIVE_PUBLICATION_STATUSES: tuple[str, ...] = tuple(
    sorted(status.value for status in PublicationStatus if status.is_active)
)


class PublicationFailure(str, Enum):
    """publication 실패 분류.

    Run failure taxonomy와 분리합니다. **구현과 검증이 성공했다는 사실은
    전달에 실패해도 변하지 않습니다.**
    """

    GATE_FAILED = "publication_gate_failed"
    WORKSPACE_DRIFT = "publication_workspace_drift"
    COMMIT_FAILED = "publication_commit_failed"
    STAGE_MISMATCH = "publication_stage_mismatch"
    REMOTE_INVALID = "publication_remote_invalid"
    AUTHENTICATION_FAILED = "publication_authentication_failed"
    PUSH_FAILED = "publication_push_failed"
    REMOTE_CONFLICT = "publication_remote_conflict"
    PR_CREATE_FAILED = "publication_pr_create_failed"
    PR_CONFLICT = "publication_pr_conflict"
    STATE_AMBIGUOUS = "publication_state_ambiguous"
    NOTHING_TO_PUBLISH = "publication_nothing_to_publish"
    # 예약 이후 remote가 다른 repository를 가리킵니다.
    REMOTE_CHANGED = "publication_remote_changed"
    # 예약 이후 승인이나 claim을 잃었습니다.
    AUTHORIZATION_LOST = "publication_authorization_lost"
    # commit 내용이 검증한 내용과 다릅니다.
    CONTENT_MISMATCH = "publication_content_mismatch"
    # 내용 지문을 계산하지 못했습니다. 증명할 수 없으면 게시하지 않습니다.
    CONTENT_DIGEST_UNAVAILABLE = "publication_content_digest_unavailable"


@dataclass(frozen=True)
class PullRequestRef:
    """만들었거나 이미 있던 draft PR."""

    number: int
    url: str
    state: str = "open"
    draft: bool = True
    node_id: str = ""
    head: str = ""
    base: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "url": self.url,
            "state": self.state,
            "draft": self.draft,
            "node_id": self.node_id,
            "head": self.head,
            "base": self.base,
        }


@dataclass(frozen=True)
class PublicationReport:
    """publication attempt 하나의 결과."""

    publication_id: str
    run_id: str
    status: PublicationStatus
    branch: str
    base_branch: str
    repository: str
    commit_sha: str = ""
    remote: str = ""
    pushed: bool = False
    pull_request: PullRequestRef | None = None
    failure: PublicationFailure | None = None
    summary: str = ""
    # 이미 있던 commit/branch/PR을 새로 만들지 않고 채택한 경우 남깁니다.
    adopted: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def published(self) -> bool:
        return self.status is PublicationStatus.PUBLISHED

    def to_dict(self) -> dict[str, Any]:
        return {
            "publication_id": self.publication_id,
            "run_id": self.run_id,
            "status": self.status.value,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "repository": self.repository,
            "commit_sha": self.commit_sha,
            "remote": self.remote,
            "pushed": self.pushed,
            "pull_request": self.pull_request.to_dict() if self.pull_request else None,
            "failure": self.failure.value if self.failure else None,
            "summary": self.summary,
            "adopted": list(self.adopted),
            "warnings": list(self.warnings),
        }


class PublicationError(Exception):
    """publication lifecycle 위반. category는 `PublicationFailure` 값입니다."""

    def __init__(
        self,
        failure: PublicationFailure,
        message: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure = failure
        self.category = failure.value
        self.message = message
        self.evidence = evidence or {}
        # 외부 상태가 모호하면 terminal로 닫지 않고 recovery로 남깁니다.
        self.recoverable = failure in _RECOVERY_FAILURES


# 이 실패들은 사람이 외부 상태를 확인해야 합니다. 자동으로 다시 시도하거나
# 덮어쓰면 안 됩니다.
_RECOVERY_FAILURES = frozenset(
    {
        PublicationFailure.REMOTE_CONFLICT,
        PublicationFailure.PR_CONFLICT,
        PublicationFailure.STATE_AMBIGUOUS,
        # 내용이 다른 commit이 이미 branch에 있습니다. 사람이 봐야 합니다.
        PublicationFailure.CONTENT_MISMATCH,
    }
)


@dataclass(frozen=True)
class GateResult:
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(self.checks.values())

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(name for name, ok in self.checks.items() if not ok)

    def to_dict(self) -> dict[str, Any]:
        return {"checks": dict(self.checks), "failed_checks": list(self.failed_checks)}
