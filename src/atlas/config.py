"""Worker 설정.

polling interval, backoff, lease TTL은 docs/adr/0008-initial-github-event-ingestion.md와
docs/specs/execution-runtime.md의 open question이므로 코드에 고정하지 않고 설정으로
노출합니다. 기본값은 단일 repository PoC 기준이며 운영 측정 후 조정해야 합니다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

DEFAULT_REPOSITORY = "hongwon1031/atlas"
DEFAULT_DATABASE_PATH = "atlas.db"

# docs/specs/issue-command-contract.md의 queue 의도 label.
QUEUE_LABEL = "atlas:queued"


@dataclass(frozen=True)
class PollingConfig:
    repository: str = DEFAULT_REPOSITORY
    interval_seconds: float = 60.0
    per_page: int = 50
    max_pages: int = 10
    # rate limit이나 provider 오류에 대한 지수 backoff.
    backoff_initial_seconds: float = 5.0
    backoff_max_seconds: float = 300.0
    backoff_multiplier: float = 2.0
    # approval/queue signal의 canonical 조합은 ingestion spec의 open question입니다.
    # 결정 전까지 label gating을 기본 비활성으로 두고 설정으로만 켭니다.
    require_queue_label: bool = False
    queue_label: str = QUEUE_LABEL

    def backoff_delay(self, attempt: int) -> float:
        """`attempt`(1부터)에 해당하는 backoff 지연을 계산합니다."""

        if attempt < 1:
            return 0.0
        delay = self.backoff_initial_seconds * (self.backoff_multiplier ** (attempt - 1))
        return min(delay, self.backoff_max_seconds)


@dataclass(frozen=True)
class ClaimConfig:
    lease_ttl_seconds: float = 900.0
    # docs/specs/execution-runtime.md는 lease expiry만으로 즉시 재실행하지 말고
    # grace period를 두라고 요구합니다. heartbeat와 process identity 확인은 아직
    # 범위 밖이므로 grace period를 확장 지점으로 남깁니다.
    grace_period_seconds: float = 0.0


@dataclass(frozen=True)
class WorkerConfig:
    database_path: str = DEFAULT_DATABASE_PATH
    polling: PollingConfig = field(default_factory=PollingConfig)
    claim: ClaimConfig = field(default_factory=ClaimConfig)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> WorkerConfig:
        env = environ if environ is not None else dict(os.environ)
        config = cls(database_path=env.get("ATLAS_DB_PATH", DEFAULT_DATABASE_PATH))

        polling = config.polling
        if repository := env.get("ATLAS_REPOSITORY", "").strip():
            polling = replace(polling, repository=repository)
        if interval := _read_float(env, "ATLAS_POLL_INTERVAL_SECONDS"):
            polling = replace(polling, interval_seconds=interval)
        if env.get("ATLAS_REQUIRE_QUEUE_LABEL", "").strip().lower() in ("1", "true", "yes"):
            polling = replace(polling, require_queue_label=True)

        claim = config.claim
        if ttl := _read_float(env, "ATLAS_LEASE_TTL_SECONDS"):
            claim = replace(claim, lease_ttl_seconds=ttl)

        return replace(config, polling=polling, claim=claim)


def _read_float(environ: dict[str, str], name: str) -> float | None:
    try:
        value = float(environ.get(name, "").strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
