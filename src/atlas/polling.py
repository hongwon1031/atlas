"""GitHub Issue polling.

docs/adr/0008-initial-github-event-ingestion.md의 polling-first 방향과
docs/specs/github-event-ingestion.md의 Candidate Polling Flow를 구현합니다.
webhook은 이 범위 밖이며, 같은 parser/validator/idempotency 경계를 재사용하도록
transport만 분리했습니다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from . import policy
from .config import PollingConfig
from .idempotency import derive_task_id
from .intake import IssueIntake, build_idempotency_key
from .issue_source import IssueLister, IssueRecord, IssueSourceError
from .store import TaskStore


@dataclass(frozen=True)
class PollReport:
    scanned: int = 0
    not_candidate: int = 0
    invalid: int = 0
    registered: tuple[str, ...] = ()
    revised: tuple[str, ...] = ()
    unchanged: int = 0
    revoked: tuple[str, ...] = ()
    error: str | None = None
    # provider가 지정한 재시도 대기 시간(초). backoff의 최소값으로 사용합니다.
    retry_after: float | None = None

    @property
    def stored(self) -> int:
        return len(self.registered) + len(self.revised)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "not_candidate": self.not_candidate,
            "invalid": self.invalid,
            "registered": list(self.registered),
            "revised": list(self.revised),
            "unchanged": self.unchanged,
            "revoked": list(self.revoked),
            "stored": self.stored,
            "error": self.error,
            "retry_after": self.retry_after,
        }


@dataclass
class _Counters:
    scanned: int = 0
    not_candidate: int = 0
    invalid: int = 0
    unchanged: int = 0
    registered: list[str] = field(default_factory=list)
    revised: list[str] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)


def candidate_rejection(issue: IssueRecord, config: PollingConfig) -> str | None:
    """후보가 아니면 사유를, 후보면 `None`을 돌려줍니다.

    Issue가 존재한다는 사실만으로 후보가 되지 않습니다
    (docs/specs/github-event-ingestion.md의 Candidate and Approval Rules).
    사유는 승인 회수 event의 근거로 기록됩니다.
    """

    if issue.is_pull_request:
        return "became_pull_request"
    if issue.state != "open":
        return "issue_not_open"
    if not issue.title.startswith(policy.ISSUE_TITLE_MARKER):
        return "task_form_marker_absent"
    if config.require_queue_label and config.queue_label not in issue.labels:
        return "queue_label_absent"
    return None


def is_task_candidate(issue: IssueRecord, config: PollingConfig) -> bool:
    return candidate_rejection(issue, config) is None


def approval_signal(issue: IssueRecord, config: PollingConfig) -> str | None:
    """관찰된 approval signal. 없으면 `None`이며 Task는 claim 대상이 아닙니다.

    `require_queue_label`을 끄더라도 label 없는 Task는 승인되지 않습니다. 설정은
    등록 여부만 바꾸고 approval 정책(ADR-008)을 우회하지 못합니다.
    """

    if config.queue_label in issue.labels:
        return f"queue_label:{config.queue_label}"
    return None


class IssuePoller:
    """후보 Issue를 찾아 valid Task만 store에 등록합니다."""

    def __init__(
        self,
        lister: IssueLister,
        intake: IssueIntake,
        store: TaskStore,
        config: PollingConfig | None = None,
    ) -> None:
        self._lister = lister
        self._intake = intake
        self._store = store
        self._config = config or PollingConfig()

    @property
    def config(self) -> PollingConfig:
        return self._config

    def poll_once(self, now: datetime | None = None) -> PollReport:
        """한 번의 polling pass. source 오류는 예외 대신 report로 돌려줍니다."""

        repository = self._config.repository
        # allowlist 검사는 네트워크 호출보다 먼저 수행합니다. worker token이
        # 접근할 수 있는 다른 repository를 경계 확인 전에 읽지 않기 위한 것입니다.
        if policy.repository_policy(repository) is None:
            return PollReport(error="repository_not_allowed")

        cursor = self._store.cursor(repository)
        try:
            issues = self._lister.list_issues(
                repository,
                since=cursor,
                per_page=self._config.per_page,
                max_pages=self._config.max_pages,
            )
        except IssueSourceError as error:
            # rate limit과 provider 오류로 Task를 실패 표시하지 않습니다.
            return PollReport(error=error.category, retry_after=error.retry_after)

        counters = _Counters()
        latest = cursor
        for issue in issues:
            counters.scanned += 1
            latest = _max_timestamp(latest, issue.updated_at)

            rejection = candidate_rejection(issue, self._config)
            if rejection is not None:
                counters.not_candidate += 1
                # 후보 조건을 잃은 Issue의 승인을 회수합니다. label 제거, Issue
                # 종료, marker 변경이 모두 여기서 reconcile됩니다.
                self._revoke(counters, issue, rejection, now)
                continue

            result = self._intake.intake_record(issue)
            if not result.is_valid:
                # invalid Issue는 저장하거나 claim하지 않습니다. 이미 저장된
                # revision이 있으면 기존 승인을 재사용하지 않도록 회수합니다.
                counters.invalid += 1
                self._revoke(counters, issue, "source_became_invalid", now)
                continue

            signal = approval_signal(issue, self._config)
            registration = self._store.register(
                result,
                build_idempotency_key(issue),
                repository=issue.repository,
                issue_number=issue.number,
                labels=issue.labels,
                approved=signal is not None,
                approval_signal=signal,
                now=now,
            )
            if registration.action == "registered":
                counters.registered.append(registration.fingerprint)
            elif registration.action == "revised":
                counters.revised.append(registration.fingerprint)
            else:
                counters.unchanged += 1

        self._store.save_cursor(repository, latest, now=now)
        return PollReport(
            scanned=counters.scanned,
            not_candidate=counters.not_candidate,
            invalid=counters.invalid,
            registered=tuple(counters.registered),
            revised=tuple(counters.revised),
            unchanged=counters.unchanged,
            revoked=tuple(counters.revoked),
        )

    def _revoke(
        self, counters: _Counters, issue: IssueRecord, reason: str, now: datetime | None
    ) -> None:
        task_id = derive_task_id(issue.number)
        if self._store.revoke_approval(task_id, reason, now=now):
            counters.revoked.append(task_id)

    def run(
        self,
        max_iterations: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_report: Callable[[PollReport], None] | None = None,
    ) -> list[PollReport]:
        """interval 간격으로 반복 polling합니다. 오류에는 지수 backoff을 적용합니다.

        `max_iterations`가 `None`이면 무한 실행이므로 report를 누적하지 않고
        `on_report`로만 흘려보냅니다. 누적하면 메모리가 무한히 증가합니다.
        """

        bounded = max_iterations is not None
        reports: list[PollReport] = []
        failures = 0
        iteration = 0
        while not bounded or iteration < max_iterations:
            iteration += 1
            report = self.poll_once()
            if bounded:
                reports.append(report)
            if on_report is not None:
                on_report(report)

            if report.error is None:
                failures = 0
                delay = self._config.interval_seconds
            else:
                failures += 1
                # provider가 Retry-After로 지정한 대기 시간을 최소값으로 존중합니다.
                delay = max(self._config.backoff_delay(failures), report.retry_after or 0.0)

            if not bounded or iteration < max_iterations:
                sleep(delay)
        return reports


def _max_timestamp(current: str | None, candidate: str) -> str | None:
    if not candidate:
        return current
    if current is None:
        return candidate
    return max(current, candidate)
