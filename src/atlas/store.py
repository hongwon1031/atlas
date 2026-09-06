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
from .schema import (
    ACTIVE_RUN_STATUSES,
    IntakeResult,
    Priority,
    Run,
    RunFailure,
    RunStatus,
    TaskStatus,
)

SCHEMA_VERSION = "3"

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
    run_id      TEXT,
    detail      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,
    claim_id         TEXT NOT NULL,
    worker_id        TEXT NOT NULL,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    heartbeat_at     TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    failure_category TEXT,
    failure_message  TEXT,
    previous_run_id  TEXT
);

-- Task 하나에 active Run은 최대 하나입니다(execution-runtime.md의 Run Boundary).
-- claim의 partial unique index와 같은 방식으로 database가 강제합니다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_active
    ON runs(task_id) WHERE status IN ('Pending', 'Running');
CREATE INDEX IF NOT EXISTS idx_runs_claim ON runs(claim_id);

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


class RunError(Exception):
    """Run lifecycle 위반. category로 분류합니다."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


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

        # schema v2 database의 events에는 run_id가 없습니다.
        event_columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(events)")
        }
        if "run_id" not in event_columns:
            self._connection.execute("ALTER TABLE events ADD COLUMN run_id TEXT")
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


    # -- run lifecycle ---------------------------------------------------

    def start_run(
        self,
        task_id: str,
        worker_id: str,
        *,
        previous_run_id: str | None = None,
        now: datetime | None = None,
    ) -> Run:
        """approved이고 claim된 Task에 새 Run을 만듭니다.

        docs/specs/execution-runtime.md의 Run Boundary를 강제합니다.

        - Task가 승인 상태여야 합니다.
        - active claim이 있어야 하고 lease가 유효해야 합니다.
        - `worker_id`가 claim의 `lease_owner`와 같아야 합니다.
        - 같은 Task에 active Run이 있으면 만들지 않습니다.

        조건을 만족하지 못하면 `RunError`를 냅니다.
        """

        moment = now or utcnow()
        stamp = to_iso(moment)

        with self._write() as connection:
            task = connection.execute(
                "SELECT fingerprint, approved, status FROM tasks "
                "WHERE task_id = ? AND is_current = 1",
                (task_id,),
            ).fetchone()
            if task is None:
                raise RunError("task_not_found", f"{task_id}에 해당하는 current Task가 없습니다.")
            if not task["approved"]:
                raise RunError(
                    "task_not_approved",
                    f"{task_id}는 승인되지 않았습니다. Run을 만들 수 없습니다.",
                )

            claim = connection.execute(
                "SELECT claim_id, lease_owner, lease_expires_at FROM claims "
                "WHERE task_id = ? AND released_at IS NULL",
                (task_id,),
            ).fetchone()
            if claim is None:
                raise RunError("no_active_claim", f"{task_id}에 active claim이 없습니다.")
            if from_iso(claim["lease_expires_at"]) <= moment:
                raise RunError(
                    "lease_expired",
                    f"{task_id}의 claim lease가 만료됐습니다. 다시 claim해야 합니다.",
                )
            if claim["lease_owner"] != worker_id:
                # lease owner가 아닌 worker가 Run을 만들면 소유권이 갈라집니다.
                raise RunError(
                    "worker_mismatch",
                    f"{worker_id}는 이 Task의 lease owner가 아닙니다.",
                )

            active = self._active_run_row(connection, task_id)
            if active is not None:
                raise RunError(
                    "active_run_exists",
                    f"{task_id}에 이미 active Run {active['run_id']}이 있습니다.",
                )

            if previous_run_id is not None:
                previous = connection.execute(
                    "SELECT status, task_id FROM runs WHERE run_id = ?", (previous_run_id,)
                ).fetchone()
                if previous is None:
                    raise RunError(
                        "previous_run_not_found", f"{previous_run_id}를 찾을 수 없습니다."
                    )
                if previous["task_id"] != task_id:
                    # retry chain은 한 Task 안에서만 이어집니다. 다른 Task의 Run을
                    # 참조하면 lineage와 감사 기록이 뒤섞입니다.
                    raise RunError(
                        "previous_run_task_mismatch",
                        f"{previous_run_id}는 {previous['task_id']}의 Run입니다. "
                        f"{task_id}의 retry로 지정할 수 없습니다.",
                    )
                if not RunStatus(previous["status"]).is_terminal:
                    raise RunError(
                        "previous_run_active",
                        f"{previous_run_id}가 아직 종료되지 않았습니다.",
                    )

            run = Run(
                run_id=f"run-{uuid.uuid4().hex[:16]}",
                task_id=task_id,
                fingerprint=task["fingerprint"],
                claim_id=claim["claim_id"],
                worker_id=worker_id,
                status=RunStatus.PENDING,
                created_at=stamp,
                heartbeat_at=stamp,
                previous_run_id=previous_run_id,
            )
            connection.execute(
                "INSERT INTO runs("
                " run_id, task_id, fingerprint, claim_id, worker_id, status,"
                " created_at, heartbeat_at, previous_run_id"
                ") VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run.run_id,
                    run.task_id,
                    run.fingerprint,
                    run.claim_id,
                    run.worker_id,
                    run.status.value,
                    run.created_at,
                    run.heartbeat_at,
                    run.previous_run_id,
                ),
            )
            self._record(
                connection,
                kind="run_started",
                moment=stamp,
                task_id=task_id,
                fingerprint=task["fingerprint"],
                claim_id=claim["claim_id"],
                run_id=run.run_id,
                detail={"worker_id": worker_id, "previous_run_id": previous_run_id},
            )
            return run

    def heartbeat(
        self, run_id: str, worker_id: str, *, now: datetime | None = None
    ) -> Run:
        """Run이 살아 있음을 기록합니다.

        첫 heartbeat는 `Pending`을 `Running`으로 올립니다. executor가 실제로
        시작됐다는 증거이기 때문입니다. terminal Run에는 허용하지 않습니다.
        """

        moment = now or utcnow()
        stamp = to_iso(moment)

        with self._write() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunError("run_not_found", f"{run_id}를 찾을 수 없습니다.")
            status = RunStatus(row["status"])
            if status.is_terminal:
                raise RunError(
                    "run_terminal",
                    f"{run_id}는 이미 {status.value} 상태입니다. heartbeat할 수 없습니다.",
                )
            if row["worker_id"] != worker_id:
                raise RunError(
                    "worker_mismatch",
                    f"{worker_id}는 {run_id}의 owner가 아닙니다.",
                )

            promoted = status is RunStatus.PENDING
            new_status = RunStatus.RUNNING
            connection.execute(
                "UPDATE runs SET status = ?, heartbeat_at = ?, "
                "started_at = COALESCE(started_at, ?) WHERE run_id = ?",
                (new_status.value, stamp, stamp, run_id),
            )
            if promoted:
                self._record(
                    connection,
                    kind="run_running",
                    moment=stamp,
                    task_id=row["task_id"],
                    run_id=run_id,
                    detail={"worker_id": worker_id},
                )
            return self._run_from_row(
                connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            )

    def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        worker_id: str | None = None,
        failure: RunFailure | None = None,
        now: datetime | None = None,
    ) -> Run:
        """Run을 terminal 상태로 전이합니다.

        `worker_id`를 주면 owner 일치를 확인합니다. reconciliation처럼 worker를
        대신해 종료할 때는 생략합니다.
        """

        if not status.is_terminal:
            raise RunError("not_terminal_status", f"{status.value}는 terminal 상태가 아닙니다.")
        if status is RunStatus.SUCCEEDED and failure is not None:
            raise RunError("unexpected_failure", "Succeeded Run에는 failure를 기록하지 않습니다.")
        if status in (RunStatus.FAILED, RunStatus.ORPHANED) and failure is None:
            raise RunError("missing_failure", f"{status.value}에는 failure 사유가 필요합니다.")

        moment = now or utcnow()
        stamp = to_iso(moment)

        with self._write() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunError("run_not_found", f"{run_id}를 찾을 수 없습니다.")
            current = RunStatus(row["status"])
            if current.is_terminal:
                raise RunError(
                    "run_terminal",
                    f"{run_id}는 이미 {current.value} 상태입니다.",
                )
            if worker_id is not None and row["worker_id"] != worker_id:
                raise RunError("worker_mismatch", f"{worker_id}는 {run_id}의 owner가 아닙니다.")

            connection.execute(
                "UPDATE runs SET status = ?, finished_at = ?, "
                "failure_category = ?, failure_message = ? WHERE run_id = ?",
                (
                    status.value,
                    stamp,
                    failure.category if failure else None,
                    failure.message if failure else None,
                    run_id,
                ),
            )
            self._record(
                connection,
                kind="run_finished",
                moment=stamp,
                task_id=row["task_id"],
                fingerprint=row["fingerprint"],
                claim_id=row["claim_id"],
                run_id=run_id,
                detail={
                    "status": status.value,
                    "from": current.value,
                    "failure": failure.to_dict() if failure else None,
                },
            )
            return self._run_from_row(
                connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            )

    def orphan_if_stale(
        self,
        run_id: str,
        *,
        observed_heartbeat_at: str,
        stale_after_seconds: float,
        failure: RunFailure,
        evidence: dict[str, Any],
        now: datetime | None = None,
    ) -> Run | None:
        """stale Run을 recovery review 대상으로 전환합니다.

        판정과 전이 사이에 worker가 살아나 heartbeat를 보낼 수 있습니다. stale
        snapshot만 믿고 전이하면 살아 있는 Run을 죽은 것으로 만듭니다. 그래서
        하나의 write transaction 안에서 다음을 모두 수행합니다.

        1. 현재 status와 heartbeat_at을 다시 읽습니다.
        2. 판정 때 본 `observed_heartbeat_at`과 다르면 회수하지 않습니다.
        3. 현재 heartbeat 기준으로도 stale한지 다시 확인합니다.
        4. 근거 event와 상태 전이를 같은 transaction에 기록합니다.

        회수하지 않으면 `None`을 돌려줍니다.
        """

        moment = now or utcnow()
        stamp = to_iso(moment)

        with self._write() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None

            current = RunStatus(row["status"])
            if current.is_terminal:
                # 다른 경로가 먼저 종료시켰습니다.
                return None
            if row["heartbeat_at"] != observed_heartbeat_at:
                # 판정 이후 heartbeat가 갱신됐습니다. 살아 있는 Run입니다.
                return None
            deadline = from_iso(row["heartbeat_at"]) + timedelta(seconds=stale_after_seconds)
            if deadline > moment:
                # 현재 시각 기준으로는 더 이상 stale하지 않습니다.
                return None

            self._record(
                connection,
                kind="run_orphaned",
                moment=stamp,
                task_id=row["task_id"],
                fingerprint=row["fingerprint"],
                claim_id=row["claim_id"],
                run_id=run_id,
                detail=evidence,
            )
            connection.execute(
                "UPDATE runs SET status = ?, finished_at = ?, "
                "failure_category = ?, failure_message = ? WHERE run_id = ?",
                (
                    RunStatus.ORPHANED.value,
                    stamp,
                    failure.category,
                    failure.message,
                    run_id,
                ),
            )
            self._record(
                connection,
                kind="run_finished",
                moment=stamp,
                task_id=row["task_id"],
                fingerprint=row["fingerprint"],
                claim_id=row["claim_id"],
                run_id=run_id,
                detail={
                    "status": RunStatus.ORPHANED.value,
                    "from": current.value,
                    "failure": failure.to_dict(),
                },
            )
            return self._run_from_row(
                connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            )

    # -- run reads -------------------------------------------------------

    def run(self, run_id: str) -> Run | None:
        row = self._connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._run_from_row(row) if row else None

    def active_run(self, task_id: str) -> Run | None:
        row = self._active_run_row(self._connection, task_id)
        return self._run_from_row(row) if row else None

    def runs(self, task_id: str | None = None, limit: int = 100) -> list[Run]:
        query = "SELECT * FROM runs"
        params: list[Any] = []
        if task_id is not None:
            query += " WHERE task_id = ?"
            params.append(task_id)
        query += " ORDER BY created_at DESC, run_id DESC LIMIT ?"
        params.append(limit)
        return [self._run_from_row(row) for row in self._connection.execute(query, params)]

    def active_runs(self) -> list[Run]:
        placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
        rows = self._connection.execute(
            f"SELECT * FROM runs WHERE status IN ({placeholders}) ORDER BY created_at ASC",
            ACTIVE_RUN_STATUSES,
        )
        return [self._run_from_row(row) for row in rows]

    def claim_for(self, claim_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()

    @staticmethod
    def _active_run_row(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
        return connection.execute(
            f"SELECT * FROM runs WHERE task_id = ? AND status IN ({placeholders})",
            (task_id, *ACTIVE_RUN_STATUSES),
        ).fetchone()

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> Run:
        return Run(
            run_id=row["run_id"],
            task_id=row["task_id"],
            fingerprint=row["fingerprint"],
            claim_id=row["claim_id"],
            worker_id=row["worker_id"],
            status=RunStatus(row["status"]),
            created_at=row["created_at"],
            heartbeat_at=row["heartbeat_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            failure_category=row["failure_category"],
            failure_message=row["failure_message"],
            previous_run_id=row["previous_run_id"],
        )

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
        run_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events"
            "(occurred_at, kind, task_id, fingerprint, claim_id, run_id, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                moment,
                kind,
                task_id,
                fingerprint,
                claim_id,
                run_id,
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
