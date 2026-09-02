"""Operational state store (SQLite).

docs/adr/0005-project-memory-storage.md의 방향(운영 상태는 relational store)과
docs/adr/0012-operational-state-store.md의 결정을 구현합니다. domain model은
`schema.py`에 있고 이 모듈은 저장과 atomicity만 담당합니다.

보장하는 invariant는 docs/specs/task-schema.md에서 옵니다.

- 한 Task에는 동시에 하나의 유효 claim lease만 존재합니다.
- 같은 source revision을 반복 관찰해도 Task를 중복 생성하지 않습니다.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .idempotency import IdempotencyKey
from .schema import IntakeResult, Priority, TaskStatus

SCHEMA_VERSION = "2"

_PRIORITY_RANK = {
    Priority.LOW: 0,
    Priority.NORMAL: 1,
    Priority.HIGH: 2,
    Priority.URGENT: 3,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    fingerprint          TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL,
    repository           TEXT NOT NULL,
    issue_number         INTEGER NOT NULL,
    issue_id             TEXT NOT NULL,
    issue_revision       TEXT NOT NULL,
    signal_type          TEXT NOT NULL,
    signal_id            TEXT NOT NULL,
    status               TEXT NOT NULL,
    priority_rank        INTEGER NOT NULL,
    labels               TEXT NOT NULL,
    task_json            TEXT NOT NULL,
    previous_fingerprint TEXT,
    is_current           INTEGER NOT NULL DEFAULT 1,
    superseded_at        TEXT,
    -- approval은 polling 시점의 필터가 아니라 지속 상태입니다. claim은 이 값을
    -- 다시 확인하고, poller는 signal이 사라지면 회수합니다.
    approved             INTEGER NOT NULL DEFAULT 0,
    approval_signal      TEXT,
    approved_at          TEXT,
    revoked_at           TEXT,
    revoke_reason        TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- Task 하나에 current revision은 최대 하나입니다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_current
    ON tasks(task_id) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS idx_tasks_issue
    ON tasks(repository, issue_number);

CREATE TABLE IF NOT EXISTS claims (
    claim_id         TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,
    claimed_by       TEXT NOT NULL,
    lease_owner      TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    claimed_at       TEXT NOT NULL,
    released_at      TEXT,
    release_reason   TEXT
);

-- Task 하나에 active claim은 최대 하나입니다. atomicity의 최종 방어선입니다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_active
    ON claims(task_id) WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    kind        TEXT NOT NULL,
    task_id     TEXT,
    fingerprint TEXT,
    claim_id    TEXT,
    detail      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_cursors (
    repository      TEXT PRIMARY KEY,
    last_updated_at TEXT,
    last_polled_at  TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Registration:
    """`register()` 결과. `action`은 registered / unchanged / revised입니다."""

    action: str
    fingerprint: str
    task_id: str
    previous_fingerprint: str | None = None
    approved: bool = False

    @property
    def created_task(self) -> bool:
        return self.action in ("registered", "revised")


@dataclass(frozen=True)
class Claim:
    claim_id: str
    task_id: str
    fingerprint: str
    claimed_by: str
    lease_owner: str
    lease_expires_at: str
    claimed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "task_id": self.task_id,
            "fingerprint": self.fingerprint,
            "claimed_by": self.claimed_by,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "claimed_at": self.claimed_at,
        }


class TaskStore:
    """Task, revision, claim, lease, event를 보존합니다."""

    def __init__(self, path: str, busy_timeout_seconds: float = 5.0) -> None:
        self.path = path
        if path != ":memory:":
            parent = Path(path).expanduser().resolve().parent
            parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, timeout=busy_timeout_seconds)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            # 여러 worker process가 같은 파일을 열 때 reader/writer 충돌을 줄입니다.
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(_SCHEMA)
        self._migrate()
        self._connection.commit()

    def _migrate(self) -> None:
        """누락된 컬럼만 추가합니다.

        schema v1 database에는 approval 컬럼이 없습니다. 기본값 0으로 추가하므로
        승인 근거가 없는 기존 Task는 자동으로 claim 대상에서 제외됩니다.
        """

        existing = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(tasks)")
        }
        for column, ddl in (
            ("approved", "approved INTEGER NOT NULL DEFAULT 0"),
            ("approval_signal", "approval_signal TEXT"),
            ("approved_at", "approved_at TEXT"),
            ("revoked_at", "revoked_at TEXT"),
            ("revoke_reason", "revoke_reason TEXT"),
        ):
            if column not in existing:
                self._connection.execute(f"ALTER TABLE tasks ADD COLUMN {ddl}")
        self._connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> TaskStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- registration ----------------------------------------------------

    def register(
        self,
        result: IntakeResult,
        key: IdempotencyKey,
        *,
        repository: str,
        issue_number: int,
        labels: tuple[str, ...] = (),
        approved: bool = False,
        approval_signal: str | None = None,
        now: datetime | None = None,
    ) -> Registration:
        """valid Task를 등록합니다. 같은 revision을 다시 등록해도 중복 생성하지 않습니다.

        `approved`는 관찰 시점의 approval signal 유무입니다. 매 pass마다 다시
        전달되므로 signal이 사라지면 승인도 유지되지 않습니다.
        """

        if not result.is_valid or result.task is None:
            raise ValueError("invalid intake result는 저장하지 않습니다")

        moment = to_iso(now or utcnow())
        fingerprint = key.fingerprint()
        task = result.task

        with self._write() as connection:
            existing = connection.execute(
                "SELECT fingerprint, is_current FROM tasks WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            current = connection.execute(
                "SELECT fingerprint FROM tasks WHERE task_id = ? AND is_current = 1",
                (key.task_id,),
            ).fetchone()

            if existing is not None and existing["is_current"] == 1:
                # 내용은 같아도 approval signal은 바뀔 수 있으므로 최신 관찰을 반영합니다.
                self._sync_approval(
                    connection, key.task_id, fingerprint, approved, approval_signal, moment
                )
                return Registration("unchanged", fingerprint, key.task_id, None, approved)

            previous = current["fingerprint"] if current is not None else None
            if previous is not None:
                # Issue가 수정되면 기존 승인과 claim을 자동으로 재사용하지 않습니다.
                connection.execute(
                    "UPDATE tasks SET is_current = 0, superseded_at = ?, updated_at = ? "
                    "WHERE fingerprint = ?",
                    (moment, moment, previous),
                )
                self._release_active(
                    connection, key.task_id, "superseded_by_revision", moment
                )

            if existing is not None:
                # 이전 revision으로 되돌아간 경우 해당 row를 다시 current로 만듭니다.
                connection.execute(
                    "UPDATE tasks SET is_current = 1, superseded_at = NULL, updated_at = ? "
                    "WHERE fingerprint = ?",
                    (moment, fingerprint),
                )
                self._sync_approval(
                    connection, key.task_id, fingerprint, approved, approval_signal, moment
                )
            else:
                connection.execute(
                    "INSERT INTO tasks("
                    " fingerprint, task_id, repository, issue_number, issue_id,"
                    " issue_revision, signal_type, signal_id, status, priority_rank,"
                    " labels, task_json, previous_fingerprint, is_current,"
                    " approved, approval_signal, approved_at, created_at, updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)",
                    (
                        fingerprint,
                        key.task_id,
                        repository,
                        issue_number,
                        key.issue_id,
                        key.issue_revision,
                        key.signal_type,
                        key.signal_id,
                        task.status.value,
                        _PRIORITY_RANK[task.priority],
                        json.dumps(list(labels), ensure_ascii=False),
                        json.dumps(task.to_dict(), ensure_ascii=False, default=str),
                        previous,
                        1 if approved else 0,
                        approval_signal if approved else None,
                        moment if approved else None,
                        moment,
                        moment,
                    ),
                )

            action = "revised" if previous is not None else "registered"
            self._record(
                connection,
                kind=f"task_{action}",
                moment=moment,
                task_id=key.task_id,
                fingerprint=fingerprint,
                detail={
                    "issue_revision": key.issue_revision,
                    "previous": previous,
                    "approved": approved,
                    "approval_signal": approval_signal if approved else None,
                },
            )
            return Registration(action, fingerprint, key.task_id, previous, approved)

    def revoke_approval(
        self, task_id: str, reason: str, now: datetime | None = None
    ) -> bool:
        """승인을 회수하고 active claim을 해제합니다.

        approval signal(label)이 사라지거나 Issue가 닫히면 poller가 호출합니다.
        회수된 Task는 claim 대상에서 제외되지만 감사를 위해 보존합니다.
        """

        moment = to_iso(now or utcnow())
        with self._write() as connection:
            row = connection.execute(
                "SELECT fingerprint FROM tasks "
                "WHERE task_id = ? AND is_current = 1 AND approved = 1",
                (task_id,),
            ).fetchone()
            if row is None:
                return False

            connection.execute(
                "UPDATE tasks SET approved = 0, revoked_at = ?, revoke_reason = ?, "
                "updated_at = ? WHERE fingerprint = ?",
                (moment, reason, moment, row["fingerprint"]),
            )
            self._release_active(connection, task_id, f"approval_revoked:{reason}", moment)
            self._record(
                connection,
                kind="approval_revoked",
                moment=moment,
                task_id=task_id,
                fingerprint=row["fingerprint"],
                detail={"reason": reason},
            )
            return True

    def _sync_approval(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        fingerprint: str,
        approved: bool,
        approval_signal: str | None,
        moment: str,
    ) -> None:
        """이미 저장된 revision의 승인 상태를 최신 관찰에 맞춥니다."""

        row = connection.execute(
            "SELECT approved FROM tasks WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        was_approved = bool(row["approved"]) if row is not None else False
        if was_approved == approved:
            return

        if approved:
            connection.execute(
                "UPDATE tasks SET approved = 1, approval_signal = ?, approved_at = ?, "
                "revoked_at = NULL, revoke_reason = NULL, updated_at = ? "
                "WHERE fingerprint = ?",
                (approval_signal, moment, moment, fingerprint),
            )
            self._record(
                connection,
                kind="approval_granted",
                moment=moment,
                task_id=task_id,
                fingerprint=fingerprint,
                detail={"approval_signal": approval_signal},
            )
        else:
            connection.execute(
                "UPDATE tasks SET approved = 0, revoked_at = ?, "
                "revoke_reason = 'approval_signal_absent', updated_at = ? "
                "WHERE fingerprint = ?",
                (moment, moment, fingerprint),
            )
            self._release_active(
                connection, task_id, "approval_revoked:approval_signal_absent", moment
            )
            self._record(
                connection,
                kind="approval_revoked",
                moment=moment,
                task_id=task_id,
                fingerprint=fingerprint,
                detail={"reason": "approval_signal_absent"},
            )

    # -- claim and lease -------------------------------------------------

    def claim(
        self,
        worker_id: str,
        lease_ttl_seconds: float,
        *,
        task_id: str | None = None,
        grace_period_seconds: float = 0.0,
        now: datetime | None = None,
    ) -> Claim | None:
        """claim 가능한 Task 하나를 원자적으로 claim합니다. 없으면 `None`입니다.

        docs/specs/github-event-ingestion.md의 "Ingestion Claim Lease" 계약입니다.
        Task 상태를 `Queued`나 `Running`으로 옮기지 않으며 executor를 실행하지
        않습니다. state machine의 실행 claim과 구분됩니다.
        """

        if lease_ttl_seconds <= 0:
            raise ValueError(f"lease_ttl_seconds는 0보다 커야 합니다: {lease_ttl_seconds!r}")
        if grace_period_seconds < 0:
            raise ValueError(
                f"grace_period_seconds는 0 이상이어야 합니다: {grace_period_seconds!r}"
            )

        moment = now or utcnow()
        stamp = to_iso(moment)
        expires = to_iso(moment + timedelta(seconds=lease_ttl_seconds))

        with self._write() as connection:
            # approval은 claim 시점에 다시 확인합니다. signal이 사라졌거나
            # 승인 근거 없이 저장된 Task(schema v1 migration 포함)는 제외됩니다.
            query = (
                "SELECT fingerprint, task_id FROM tasks "
                "WHERE is_current = 1 AND approved = 1 AND status = ?"
            )
            params: list[Any] = [TaskStatus.DRAFT.value]
            if task_id is not None:
                query += " AND task_id = ?"
                params.append(task_id)
            query += " ORDER BY priority_rank DESC, created_at ASC"

            for row in connection.execute(query, params).fetchall():
                if not self._is_claimable(
                    connection, row["task_id"], moment, grace_period_seconds
                ):
                    continue

                claim_id = f"claim-{uuid.uuid4().hex[:16]}"
                connection.execute(
                    "INSERT INTO claims("
                    " claim_id, task_id, fingerprint, claimed_by, lease_owner,"
                    " lease_expires_at, claimed_at"
                    ") VALUES (?,?,?,?,?,?,?)",
                    (
                        claim_id,
                        row["task_id"],
                        row["fingerprint"],
                        worker_id,
                        worker_id,
                        expires,
                        stamp,
                    ),
                )
                connection.execute(
                    "UPDATE tasks SET updated_at = ? WHERE fingerprint = ?",
                    (stamp, row["fingerprint"]),
                )
                self._record(
                    connection,
                    kind="task_claimed",
                    moment=stamp,
                    task_id=row["task_id"],
                    fingerprint=row["fingerprint"],
                    claim_id=claim_id,
                    detail={"lease_expires_at": expires, "claimed_by": worker_id},
                )
                return Claim(
                    claim_id=claim_id,
                    task_id=row["task_id"],
                    fingerprint=row["fingerprint"],
                    claimed_by=worker_id,
                    lease_owner=worker_id,
                    lease_expires_at=expires,
                    claimed_at=stamp,
                )
        return None

    def release(self, claim_id: str, reason: str, now: datetime | None = None) -> bool:
        moment = to_iso(now or utcnow())
        with self._write() as connection:
            row = connection.execute(
                "SELECT task_id, fingerprint FROM claims "
                "WHERE claim_id = ? AND released_at IS NULL",
                (claim_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE claims SET released_at = ?, release_reason = ? WHERE claim_id = ?",
                (moment, reason, claim_id),
            )
            self._record(
                connection,
                kind="claim_released",
                moment=moment,
                task_id=row["task_id"],
                fingerprint=row["fingerprint"],
                claim_id=claim_id,
                detail={"reason": reason},
            )
            return True

    def renew_lease(
        self, claim_id: str, lease_ttl_seconds: float, now: datetime | None = None
    ) -> str | None:
        """lease를 연장합니다. heartbeat 구현의 확장 지점입니다."""

        if lease_ttl_seconds <= 0:
            raise ValueError(f"lease_ttl_seconds는 0보다 커야 합니다: {lease_ttl_seconds!r}")

        moment = now or utcnow()
        expires = to_iso(moment + timedelta(seconds=lease_ttl_seconds))
        with self._write() as connection:
            row = connection.execute(
                "SELECT task_id, fingerprint, lease_expires_at FROM claims "
                "WHERE claim_id = ? AND released_at IS NULL",
                (claim_id,),
            ).fetchone()
            if row is None or from_iso(row["lease_expires_at"]) <= moment:
                return None
            connection.execute(
                "UPDATE claims SET lease_expires_at = ? WHERE claim_id = ?",
                (expires, claim_id),
            )
            self._record(
                connection,
                kind="lease_renewed",
                moment=to_iso(moment),
                task_id=row["task_id"],
                fingerprint=row["fingerprint"],
                claim_id=claim_id,
                detail={"lease_expires_at": expires},
            )
            return expires

    def active_claim(self, task_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM claims WHERE task_id = ? AND released_at IS NULL",
            (task_id,),
        ).fetchone()

    # -- reads -----------------------------------------------------------

    def current_tasks(self) -> list[sqlite3.Row]:
        return self._connection.execute(
            "SELECT * FROM tasks WHERE is_current = 1 "
            "ORDER BY priority_rank DESC, created_at ASC"
        ).fetchall()

    def task_by_fingerprint(self, fingerprint: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM tasks WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()

    def revisions(self, task_id: str) -> list[sqlite3.Row]:
        return self._connection.execute(
            "SELECT * FROM tasks WHERE task_id = ? ORDER BY created_at ASC", (task_id,)
        ).fetchall()

    def events(self, limit: int = 100) -> list[sqlite3.Row]:
        return self._connection.execute(
            "SELECT * FROM events ORDER BY event_id DESC LIMIT ?", (limit,)
        ).fetchall()

    def cursor(self, repository: str) -> str | None:
        row = self._connection.execute(
            "SELECT last_updated_at FROM poll_cursors WHERE repository = ?", (repository,)
        ).fetchone()
        return row["last_updated_at"] if row else None

    def save_cursor(
        self, repository: str, last_updated_at: str | None, now: datetime | None = None
    ) -> None:
        moment = to_iso(now or utcnow())
        with self._write() as connection:
            connection.execute(
                "INSERT INTO poll_cursors(repository, last_updated_at, last_polled_at) "
                "VALUES (?,?,?) ON CONFLICT(repository) DO UPDATE SET "
                "last_updated_at = excluded.last_updated_at, "
                "last_polled_at = excluded.last_polled_at",
                (repository, last_updated_at, moment),
            )

    # -- internals -------------------------------------------------------

    def _write(self):
        """`BEGIN IMMEDIATE` 트랜잭션. write lock을 즉시 잡아 race를 직렬화합니다."""

        return _ImmediateTransaction(self._connection)

    def _is_claimable(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        moment: datetime,
        grace_period_seconds: float,
    ) -> bool:
        row = connection.execute(
            "SELECT claim_id, lease_expires_at, lease_owner FROM claims "
            "WHERE task_id = ? AND released_at IS NULL",
            (task_id,),
        ).fetchone()
        if row is None:
            return True

        deadline = from_iso(row["lease_expires_at"]) + timedelta(seconds=grace_period_seconds)
        if deadline > moment:
            return False

        # 만료된 lease는 이전 owner, expiry, 판단 근거를 event로 남기고 회수합니다.
        stamp = to_iso(moment)
        connection.execute(
            "UPDATE claims SET released_at = ?, release_reason = 'lease_expired' "
            "WHERE claim_id = ?",
            (stamp, row["claim_id"]),
        )
        self._record(
            connection,
            kind="lease_expired",
            moment=stamp,
            task_id=task_id,
            claim_id=row["claim_id"],
            detail={
                "previous_lease_owner": row["lease_owner"],
                "lease_expired_at": row["lease_expires_at"],
                "grace_period_seconds": grace_period_seconds,
            },
        )
        return True

    def _release_active(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        reason: str,
        moment: str,
    ) -> None:
        row = connection.execute(
            "SELECT claim_id, fingerprint FROM claims "
            "WHERE task_id = ? AND released_at IS NULL",
            (task_id,),
        ).fetchone()
        if row is None:
            return
        connection.execute(
            "UPDATE claims SET released_at = ?, release_reason = ? WHERE claim_id = ?",
            (moment, reason, row["claim_id"]),
        )
        self._record(
            connection,
            kind="claim_released",
            moment=moment,
            task_id=task_id,
            fingerprint=row["fingerprint"],
            claim_id=row["claim_id"],
            detail={"reason": reason},
        )

    @staticmethod
    def _record(
        connection: sqlite3.Connection,
        *,
        kind: str,
        moment: str,
        task_id: str | None = None,
        fingerprint: str | None = None,
        claim_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events(occurred_at, kind, task_id, fingerprint, claim_id, detail) "
            "VALUES (?,?,?,?,?,?)",
            (
                moment,
                kind,
                task_id,
                fingerprint,
                claim_id,
                json.dumps(detail or {}, ensure_ascii=False, default=str),
            ),
        )


class _ImmediateTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._connection.execute("BEGIN IMMEDIATE")
        return self._connection

    def __exit__(self, exc_type, *_: object) -> None:
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()
