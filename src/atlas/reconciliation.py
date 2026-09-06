"""Restart reconciliation.

docs/specs/execution-runtime.md의 "Restart and Recovery"를 구현합니다.

핵심 요구는 두 가지입니다.

- lease 만료나 heartbeat 중단만으로 즉시 재실행하지 않습니다.
- process 상태를 증명할 수 없으면 새 side effect를 허용하지 않고 recovery
  review 상태로 기록합니다. 여기서는 `Orphaned`입니다.

process identity(PID, start time) 확인은 executor process를 만드는 후속
slice의 범위입니다. 지금은 heartbeat와 claim lease만으로 판단하며, 판단 근거를
event에 남겨 나중에 감사할 수 있게 합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import RunConfig
from .schema import Run, RunFailure, RunStatus
from .store import TaskStore, from_iso, utcnow

# 재실행이 아니라 사람 확인이 필요하다는 뜻의 실패 분류입니다.
ORPHAN_FAILURE_CATEGORY = "worker_lost"


@dataclass(frozen=True)
class RunVerdict:
    """Run 하나에 대한 판정과 근거."""

    run_id: str
    task_id: str
    action: str
    reason: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "action": self.action,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ReconcileReport:
    checked: int = 0
    healthy: tuple[str, ...] = ()
    orphaned: tuple[str, ...] = ()
    verdicts: tuple[RunVerdict, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "healthy": list(self.healthy),
            "orphaned": list(self.orphaned),
            "verdicts": [verdict.to_dict() for verdict in self.verdicts],
        }


@dataclass
class _Counters:
    healthy: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    verdicts: list[RunVerdict] = field(default_factory=list)


class RunReconciler:
    """active Run을 훑어 stale한 것을 recovery review로 넘깁니다."""

    def __init__(self, store: TaskStore, config: RunConfig | None = None) -> None:
        self._store = store
        self._config = config or RunConfig()

    @property
    def config(self) -> RunConfig:
        return self._config

    def reconcile(self, now: datetime | None = None) -> ReconcileReport:
        moment = now or utcnow()
        counters = _Counters()
        active = self._store.active_runs()

        for run in active:
            verdict = self.evaluate(run, moment)
            counters.verdicts.append(verdict)
            if verdict.action == "orphan":
                self._store.mark_orphaned(
                    run.run_id,
                    RunFailure(ORPHAN_FAILURE_CATEGORY, verdict.reason),
                    verdict.evidence,
                    now=moment,
                )
                counters.orphaned.append(run.run_id)
            else:
                counters.healthy.append(run.run_id)

        return ReconcileReport(
            checked=len(active),
            healthy=tuple(counters.healthy),
            orphaned=tuple(counters.orphaned),
            verdicts=tuple(counters.verdicts),
        )

    def evaluate(self, run: Run, now: datetime | None = None) -> RunVerdict:
        """Run 하나를 판정합니다. 상태를 바꾸지 않으므로 단독 조회에 쓸 수 있습니다."""

        moment = now or utcnow()
        heartbeat_at = from_iso(run.heartbeat_at)
        heartbeat_age = (moment - heartbeat_at).total_seconds()
        deadline = heartbeat_at + timedelta(seconds=self._config.stale_after_seconds)

        claim = self._store.claim_for(run.claim_id)
        claim_released = claim is None or claim["released_at"] is not None
        lease_expired = (
            claim is not None and from_iso(claim["lease_expires_at"]) <= moment
        )

        evidence: dict[str, Any] = {
            "status": run.status.value,
            "worker_id": run.worker_id,
            "heartbeat_at": run.heartbeat_at,
            "heartbeat_age_seconds": round(heartbeat_age, 3),
            "stale_after_seconds": self._config.stale_after_seconds,
            "claim_id": run.claim_id,
            "claim_released": claim_released,
            "lease_expired": lease_expired,
            # process identity 확인은 executor process가 생긴 뒤에 가능합니다.
            "process_identity_checked": False,
        }

        heartbeat_stale = deadline <= moment
        if not heartbeat_stale:
            return RunVerdict(
                run.run_id,
                run.task_id,
                "keep",
                "heartbeat가 stale threshold 안에 있습니다.",
                evidence,
            )

        # heartbeat가 끊겼고 claim 근거도 사라졌거나 만료됐습니다. 이 Run을
        # 계속 살아 있다고 볼 근거가 없으므로 recovery review로 넘깁니다.
        if claim_released:
            reason = "heartbeat가 중단됐고 claim이 이미 해제됐습니다."
        elif lease_expired:
            reason = "heartbeat가 중단됐고 claim lease도 만료됐습니다."
        else:
            reason = "heartbeat가 stale threshold를 초과했습니다."

        return RunVerdict(run.run_id, run.task_id, "orphan", reason, evidence)


def is_stale(run: Run, config: RunConfig, now: datetime | None = None) -> bool:
    moment = now or utcnow()
    return (
        not RunStatus(run.status).is_terminal
        and from_iso(run.heartbeat_at) + timedelta(seconds=config.stale_after_seconds) <= moment
    )
