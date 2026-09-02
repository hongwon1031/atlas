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


def is_task_candidate(issue: IssueRecord, config: PollingConfig) -> bool:
    """Issue Form marker와 선택적 queue label로 후보를 좁힙니다.

    Issue가 존재한다는 사실만으로 후보가 되지 않습니다
    (docs/specs/github-event-ingestion.md의 Candidate and Approval Rules).
    """

    if issue.is_pull_request or issue.state != "open":
        return False
    if not issue.title.startswith(policy.ISSUE_TITLE_MARKER):
        return False
    if config.require_queue_label and config.queue_label not in issue.labels:
        return False
    return True


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
            issues = self._lister.list_open_issues(
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

            if not is_task_candidate(issue, self._config):
                counters.not_candidate += 1
                continue

            result = self._intake.intake_record(issue)
            if not result.is_valid:
                # invalid Issue는 저장하거나 claim하지 않습니다.
                counters.invalid += 1
                continue

            registration = self._store.register(
                result,
                build_idempotency_key(issue),
                repository=issue.repository,
                issue_number=issue.number,
                labels=issue.labels,
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
        )

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
