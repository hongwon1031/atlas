"""Idempotency key와 in-process 중복 방지.

정규 정의는 docs/specs/github-event-ingestion.md의 "Idempotency Keys"입니다.
이 슬라이스는 persistence를 구현하지 않으므로 cache는 한 process 수명 동안만
유효합니다.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

# 이 슬라이스는 comment command와 label을 처리하지 않습니다. 사람이 직접 요청한
# intake임을 signal_type으로 남깁니다.
MANUAL_INTAKE_SIGNAL = "manual_intake"


@dataclass(frozen=True)
class IdempotencyKey:
    repository_id: str
    issue_id: str
    issue_revision: str
    signal_type: str
    signal_id: str
    task_id: str

    def fingerprint(self) -> str:
        joined = "\x1f".join(
            (
                self.repository_id,
                self.issue_id,
                self.issue_revision,
                self.signal_type,
                self.signal_id,
                self.task_id,
            )
        )
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_issue_revision(title: str, body: str) -> str:
    """Issue 내용의 정규화된 hash.

    줄 끝 공백과 개행 표기 차이는 revision 변경으로 보지 않습니다.
    """

    normalized = "\n".join(
        line.rstrip() for line in f"{title}\n\n{body}".replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def derive_task_id(issue_number: int) -> str:
    """Issue 번호에서 Task ID를 유도합니다.

    docs/specs/task-schema.md의 open question("Issue 번호 유도 vs 별도 sequence")은
    아직 미결입니다. persistence가 없는 동안에는 결정적 ID가 필요하므로 번호를
    사용하고, 결정이 나면 이 함수만 교체합니다.
    """

    return f"ATLAS-{issue_number:04d}"


class InProcessIntakeCache:
    """같은 source identity를 다시 보면 이전 결과를 그대로 돌려줍니다."""

    def __init__(self) -> None:
        self._entries: dict[str, Any] = {}

    def get(self, key: IdempotencyKey) -> Any | None:
        return self._entries.get(key.fingerprint())

    def put(self, key: IdempotencyKey, result: Any) -> None:
        self._entries.setdefault(key.fingerprint(), result)

    def __len__(self) -> int:
        return len(self._entries)
