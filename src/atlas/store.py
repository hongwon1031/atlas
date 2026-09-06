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
from .executor import ExecutionStatus
from .validation_models import ValidationStatus
from .schema import (
    ACTIVE_RUN_STATUSES,
    IntakeResult,
    Priority,
    Run,
    RunFailure,
    RunStatus,
    TaskStatus,
    WorkspaceStatus,
)

SCHEMA_VERSION = "7"

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
    execution_id TEXT,
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
    previous_run_id  TEXT,
    -- workspace lifecycle. git side effect를 DB transaction 안에서 오래 잡지
    -- 않으려고 상태를 단계로 나눕니다(none -> preparing -> ready -> removed).
    -- preparing에서 실패해도 branch/worktree_path가 남아 orphan을 식별합니다.
    workspace_status     TEXT NOT NULL DEFAULT 'none',
    branch               TEXT,
    worktree_path        TEXT,
    base_branch          TEXT,
    base_revision        TEXT,
    workspace_created_at TEXT,
    workspace_removed_at TEXT,
    workspace_error      TEXT
);

-- Task 하나에 active Run은 최대 하나입니다(execution-runtime.md의 Run Boundary).
-- claim의 partial unique index와 같은 방식으로 database가 강제합니다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_active
    ON runs(task_id)
    WHERE status IN ('Pending', 'Running', 'AwaitingValidation', 'Validating');
CREATE INDEX IF NOT EXISTS idx_runs_claim ON runs(claim_id);

CREATE TABLE IF NOT EXISTS executions (
    execution_id         TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL,
    task_id              TEXT NOT NULL,
    executor_name        TEXT NOT NULL,
    executor_provider    TEXT NOT NULL,
    status               TEXT NOT NULL,
    worker_id            TEXT NOT NULL,
    cwd                  TEXT NOT NULL,
    command              TEXT NOT NULL,
    timeout_seconds      REAL NOT NULL,
    cancellation_state   TEXT NOT NULL DEFAULT 'none',
    -- process identity. PID만으로는 PID 재사용을 구분할 수 없습니다.
    process_id           INTEGER,
    process_identity     TEXT,
    process_started_at   TEXT,
    process_finished_at  TEXT,
    process_exit_code    INTEGER,
    stdout_path          TEXT,
    stdout_bytes         INTEGER,
    stdout_truncated     INTEGER,
    stderr_path          TEXT,
    stderr_bytes         INTEGER,
    stderr_truncated     INTEGER,
    failure_category     TEXT,
    executor_error       TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- Run 하나에 active execution은 최대 하나입니다. DB reservation과 spawn 사이의
-- race를 database가 최종적으로 막습니다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_active
    ON executions(run_id) WHERE status IN ('Starting', 'Running', 'Cancelling');
CREATE INDEX IF NOT EXISTS idx_executions_run ON executions(run_id);

-- validation attempt. Run 하나에 여러 번 시도할 수 있으므로 별도 table입니다.
-- event만으로는 restart 후 "어디까지 끝났는가"를 복원할 수 없습니다.
CREATE TABLE IF NOT EXISTS validations (
    validation_id    TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    task_id          TEXT NOT NULL,
    worker_id        TEXT NOT NULL,
    status           TEXT NOT NULL,
    outcome          TEXT,
    cwd              TEXT NOT NULL,
    plan_json        TEXT NOT NULL,
    summary          TEXT,
    warnings         TEXT,
    failure_category TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- Run 하나에 active validation은 최대 하나입니다. 중복 시작을 database가
-- 최종적으로 막습니다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_validations_active
    ON validations(run_id) WHERE status IN ('Starting', 'Running');
CREATE INDEX IF NOT EXISTS idx_validations_run ON validations(run_id);

-- step 단위 결과. 어디까지 끝났는지, 지금 어떤 process가 도는지 알 수 있어야
-- restart 후 판정할 수 있습니다.
CREATE TABLE IF NOT EXISTS validation_steps (
    step_id            TEXT PRIMARY KEY,
    validation_id      TEXT NOT NULL,
    run_id             TEXT NOT NULL,
    position           INTEGER NOT NULL,
    name               TEXT NOT NULL,
    kind               TEXT NOT NULL,
    required           INTEGER NOT NULL,
    status             TEXT NOT NULL,
    command            TEXT NOT NULL,
    exit_code          INTEGER,
    duration_seconds   REAL,
    process_id         INTEGER,
    process_identity   TEXT,
    process_started_at TEXT,
    stdout_path        TEXT,
    stdout_bytes       INTEGER,
    stdout_truncated   INTEGER,
    stderr_path        TEXT,
    stderr_bytes       INTEGER,
    stderr_truncated   INTEGER,
    reason             TEXT,
    evidence           TEXT,
    started_at         TEXT,
    finished_at        TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_validation_steps_position
    ON validation_steps(validation_id, position);
CREATE INDEX IF NOT EXISTS idx_validation_steps_run ON validation_steps(run_id);

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


class ValidationError(RunError):
    """validation lifecycle 위반."""


class ValidationConflict(ValidationError):
    """validation을 시작할 수 없거나 기대한 단계가 아닙니다."""

    def __init__(self, category: str, message: str, validation: dict | None = None) -> None:
        super().__init__(category, message)
        self.validation = validation


class ExecutionError(RunError):
    """execution lifecycle 위반."""


class ExecutionConflict(ExecutionError):
    """이미 active execution이 있거나 기대한 단계가 아닙니다."""

    def __init__(self, category: str, message: str, execution: dict | None = None) -> None:
        super().__init__(category, message)
        self.execution = execution


class WorkspaceConflict(RunError):
    """이미 workspace가 있거나 기대한 단계가 아닙니다.

    호출자가 idempotent하게 처리할 수 있도록 현재 Run을 함께 전달합니다.
    """

    def __init__(self, category: str, message: str, run: "Run | None" = None) -> None:
        super().__init__(category, message)
        self.run = run


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

        # schema v4 database의 events에는 execution_id가 없습니다.
        event_columns2 = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(events)")
        }
        if "execution_id" not in event_columns2:
            self._connection.execute("ALTER TABLE events ADD COLUMN execution_id TEXT")
        if "validation_id" not in event_columns2:
            self._connection.execute("ALTER TABLE events ADD COLUMN validation_id TEXT")

        # schema v3 database의 runs에는 workspace 컬럼이 없습니다.
        run_columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(runs)")
        }
        for column, ddl in (
            ("workspace_status", "workspace_status TEXT NOT NULL DEFAULT 'none'"),
            ("branch", "branch TEXT"),
            ("worktree_path", "worktree_path TEXT"),
            ("base_branch", "base_branch TEXT"),
            ("base_revision", "base_revision TEXT"),
            ("workspace_created_at", "workspace_created_at TEXT"),
            ("workspace_removed_at", "workspace_removed_at TEXT"),
            ("workspace_error", "workspace_error TEXT"),
        ):
            if column not in run_columns:
                self._connection.execute(f"ALTER TABLE runs ADD COLUMN {ddl}")

        # schema v2 database의 events에는 run_id가 없습니다.
        event_columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(events)")
        }
        if "run_id" not in event_columns:
            self._connection.execute("ALTER TABLE events ADD COLUMN run_id TEXT")
        # schema v5의 active Run 인덱스는 AwaitingValidation을 모릅니다. 그대로
        # 두면 구현을 마친 Run이 슬롯을 지키지 못해 같은 Task로 새 Run이
        # 시작될 수 있습니다. 정의가 다르면 다시 만듭니다.
        index_sql = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_runs_active'"
        ).fetchone()
        if index_sql and "Validating" not in (index_sql["sql"] or ""):
            self._connection.execute("DROP INDEX idx_runs_active")
            self._connection.execute(
                "CREATE UNIQUE INDEX idx_runs_active ON runs(task_id) WHERE status IN "
                "('Pending', 'Running', 'AwaitingValidation', 'Validating')"
            )

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
            if not status.expects_heartbeat:
                # AwaitingValidation처럼 process가 없는 상태입니다. heartbeat를
                # 받으면 살아 있다는 잘못된 근거가 생깁니다.
                raise RunError(
                    "heartbeat_not_expected",
                    f"{run_id}는 {status.value} 상태라 heartbeat 대상이 아닙니다.",
                )
            if row["worker_id"] != worker_id:
                raise RunError(
                    "worker_mismatch",
                    f"{worker_id}는 {run_id}의 owner가 아닙니다.",
                )

            promoted = status is RunStatus.PENDING
            # Pending일 때만 올립니다. Validating을 Running으로 되돌리면
            # 어느 단계인지 알 수 없게 됩니다.
            new_status = RunStatus.RUNNING if promoted else status
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

    def await_validation(
        self,
        run_id: str,
        *,
        worker_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> Run:
        """구현을 마친 Run을 `AwaitingValidation`으로 전이합니다.

        terminal이 아닙니다. executor process는 끝났지만 결과를 아무도
        검증하지 않았습니다. heartbeat 대상에서 빠지므로 staleness 판정이
        정상 결과를 `Orphaned`로 만들지 않습니다.

        claim과 workspace는 유지합니다. 다음 validation slice가 같은 worktree
        에서 이어서 작업합니다.
        """

        moment = now or utcnow()
        stamp = to_iso(moment)

        with self._write() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunError("run_not_found", f"{run_id}를 찾을 수 없습니다.")
            current = RunStatus(row["status"])
            if current is RunStatus.AWAITING_VALIDATION:
                # 같은 결과를 다시 보고해도 안전합니다.
                return self._run_from_row(row)
            if current.is_terminal:
                raise RunError(
                    "run_terminal",
                    f"{run_id}는 이미 {current.value} 상태입니다.",
                )
            if worker_id is not None and row["worker_id"] != worker_id:
                raise RunError("worker_mismatch", f"{worker_id}는 {run_id}의 owner가 아닙니다.")

            connection.execute(
                "UPDATE runs SET status = ?, heartbeat_at = ? WHERE run_id = ?",
                (RunStatus.AWAITING_VALIDATION.value, stamp, run_id),
            )
            self._record(
                connection,
                kind="run_awaiting_validation",
                moment=stamp,
                task_id=row["task_id"],
                fingerprint=row["fingerprint"],
                claim_id=row["claim_id"],
                run_id=run_id,
                detail=evidence or {},
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
            if not current.expects_heartbeat:
                # heartbeat를 기대하지 않는 상태입니다. 예를 들어
                # AwaitingValidation은 executor가 이미 정상 종료했으므로
                # heartbeat가 멈춘 것이 정상입니다. stale이 아닙니다.
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


    # -- workspace lifecycle ---------------------------------------------

    def begin_workspace(
        self,
        run_id: str,
        *,
        branch: str,
        worktree_path: str,
        base_branch: str,
        base_revision: str,
        now: datetime | None = None,
    ) -> Run:
        """git을 건드리기 전에 의도를 먼저 기록합니다.

        git side effect를 DB transaction 안에서 잡지 않으려고 lifecycle을
        나눕니다. `preparing` 기록이 먼저 남으므로 git 도중 실패해도 어떤
        branch와 경로를 정리해야 하는지 알 수 있습니다.

        이미 `ready`이거나 `preparing`이면 `WorkspaceConflict`를 냅니다.
        """

        moment = to_iso(now or utcnow())
        with self._write() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunError("run_not_found", f"{run_id}를 찾을 수 없습니다.")
            if RunStatus(row["status"]).is_terminal:
                raise RunError(
                    "run_terminal", f"{run_id}는 이미 종료돼 workspace를 만들 수 없습니다."
                )

            status = WorkspaceStatus(row["workspace_status"])
            if status in (WorkspaceStatus.PREPARING, WorkspaceStatus.READY):
                raise WorkspaceConflict(
                    "workspace_already_exists",
                    f"{run_id}에 이미 workspace가 있습니다 ({status.value}).",
                    self._run_from_row(row),
                )

            connection.execute(
                "UPDATE runs SET workspace_status = ?, branch = ?, worktree_path = ?, "
                "base_branch = ?, base_revision = ?, workspace_error = NULL, "
                "workspace_removed_at = NULL WHERE run_id = ?",
                (
                    WorkspaceStatus.PREPARING.value,
                    branch,
                    worktree_path,
                    base_branch,
                    base_revision,
                    run_id,
                ),
            )
            self._record(
                connection,
                kind="workspace_preparing",
                moment=moment,
                task_id=row["task_id"],
                run_id=run_id,
                detail={"branch": branch, "base_branch": base_branch, "base_revision": base_revision},
            )
            return self._run_from_row(
                connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            )

    def attach_workspace(
        self, run_id: str, evidence: dict[str, Any] | None = None, now: datetime | None = None
    ) -> Run:
        """git 작업이 끝나고 검증까지 통과한 workspace를 `ready`로 확정합니다."""

        moment = to_iso(now or utcnow())
        with self._write() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunError("run_not_found", f"{run_id}를 찾을 수 없습니다.")
            if WorkspaceStatus(row["workspace_status"]) is not WorkspaceStatus.PREPARING:
                raise WorkspaceConflict(
                    "workspace_not_preparing",
                    f"{run_id}의 workspace가 preparing 상태가 아닙니다.",
                    self._run_from_row(row),
                )
            connection.execute(
                "UPDATE runs SET workspace_status = ?, workspace_created_at = ? WHERE run_id = ?",
                (WorkspaceStatus.READY.value, moment, run_id),
            )
            self._record(
                connection,
                kind="workspace_ready",
                moment=moment,
                task_id=row["task_id"],
                run_id=run_id,
                detail={"branch": row["branch"], **(evidence or {})},
            )
            return self._run_from_row(
                connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            )

    def fail_workspace(
        self, run_id: str, error: dict[str, Any], now: datetime | None = None
    ) -> Run | None:
        """workspace 준비 실패를 기록합니다.

        `preparing` 기록과 branch/경로를 남겨 두어 orphan cleanup이 대상을
        식별할 수 있게 합니다. `error`는 이미 redaction된 값이어야 합니다.
        """

        moment = to_iso(now or utcnow())
        with self._write() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE runs SET workspace_status = ?, workspace_error = ? WHERE run_id = ?",
                (
                    WorkspaceStatus.FAILED.value,
                    json.dumps(error, ensure_ascii=False, default=str),
                    run_id,
                ),
            )
            self._record(
                connection,
                kind="workspace_failed",
                moment=moment,
                task_id=row["task_id"],
                run_id=run_id,
                detail=error,
            )
            return self._run_from_row(
                connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            )

    def release_workspace(
        self,
        run_id: str,
        *,
        branch_kept: bool,
        reason: str,
        now: datetime | None = None,
    ) -> Run:
        """worktree 제거를 기록합니다. branch 보존 여부를 함께 남깁니다."""

        moment = to_iso(now or utcnow())
        with self._write() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunError("run_not_found", f"{run_id}를 찾을 수 없습니다.")
            connection.execute(
                "UPDATE runs SET workspace_status = ?, workspace_removed_at = ? WHERE run_id = ?",
                (WorkspaceStatus.REMOVED.value, moment, run_id),
            )
            self._record(
                connection,
                kind="workspace_removed",
                moment=moment,
                task_id=row["task_id"],
                run_id=run_id,
                detail={"branch": row["branch"], "branch_kept": branch_kept, "reason": reason},
            )
            return self._run_from_row(
                connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            )

    def record_workspace_event(
        self,
        run_id: str,
        kind: str,
        detail: dict[str, Any],
        now: datetime | None = None,
    ) -> None:
        """cleanup 실패나 reconciliation 판정을 감사 기록으로 남깁니다."""

        moment = to_iso(now or utcnow())
        with self._write() as connection:
            row = connection.execute(
                "SELECT task_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            self._record(
                connection,
                kind=kind,
                moment=moment,
                task_id=row["task_id"] if row else None,
                run_id=run_id,
                detail=detail,
            )

    def runs_with_workspace(self) -> list[Run]:
        """정리 대상이 될 수 있는 workspace를 가진 Run을 돌려줍니다."""

        rows = self._connection.execute(
            "SELECT * FROM runs WHERE workspace_status IN (?, ?, ?) ORDER BY created_at ASC",
            (
                WorkspaceStatus.PREPARING.value,
                WorkspaceStatus.READY.value,
                WorkspaceStatus.FAILED.value,
            ),
        )
        return [self._run_from_row(row) for row in rows]

    def run_owning_branch(self, branch: str) -> Run | None:
        """DB provenance 확인용. Atlas가 이 branch를 만들었는지 봅니다."""

        row = self._connection.execute(
            "SELECT * FROM runs WHERE branch = ? ORDER BY created_at DESC LIMIT 1", (branch,)
        ).fetchone()
        return self._run_from_row(row) if row else None


    # -- execution lifecycle ---------------------------------------------

    def reserve_execution(
        self,
        run_id: str,
        *,
        task_id: str,
        executor_name: str,
        executor_provider: str,
        worker_id: str,
        cwd: str,
        command: list[str],
        timeout_seconds: float,
        now: datetime | None = None,
    ) -> str:
        """process를 띄우기 전에 실행 의도를 먼저 기록합니다.

        spawn을 DB transaction 안에서 잡지 않으려고 단계를 나눕니다. `Starting`
        기록이 먼저 남으므로 spawn 직전이나 직후에 죽어도 reconciliation이
        "process를 만들려다 만 Run"을 식별할 수 있습니다.

        active execution이 이미 있으면 `ExecutionConflict`를 냅니다. partial
        unique index가 동시 예약을 database 수준에서 막습니다.

        `command`는 이미 redaction된 값이어야 합니다.
        """

        moment = now or utcnow()
        stamp = to_iso(moment)
        execution_id = f"exec-{uuid.uuid4().hex[:16]}"
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM executions WHERE run_id = ? AND status IN "
                "('Starting','Running','Cancelling')",
                (run_id,),
            ).fetchone()
            if existing is not None:
                raise ExecutionConflict(
                    "execution_already_active",
                    f"{run_id}에 이미 active execution {existing['execution_id']}이 있습니다.",
                    self._execution_from_row(existing),
                )

            # 실행 근거를 예약과 같은 transaction에서 확인합니다. gate와 예약
            # 사이에 승인이 회수되거나 claim이 풀리는 창을 좁힙니다.
            failed = self._reservation_guards(connection, run_id, worker_id, moment)
            if failed:
                raise ExecutionConflict(
                    "reservation_guard_failed",
                    f"예약 시점 확인에 실패했습니다: {', '.join(failed)}",
                    {"failed_checks": failed},
                )
            connection.execute(
                "INSERT INTO executions("
                " execution_id, run_id, task_id, executor_name, executor_provider,"
                " status, worker_id, cwd, command, timeout_seconds,"
                " created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    execution_id,
                    run_id,
                    task_id,
                    executor_name,
                    executor_provider,
                    ExecutionStatus.STARTING.value,
                    worker_id,
                    cwd,
                    json.dumps(command, ensure_ascii=False),
                    timeout_seconds,
                    stamp,
                    stamp,
                ),
            )
            self._record(
                connection,
                kind="execution_reserved",
                moment=stamp,
                task_id=task_id,
                run_id=run_id,
                execution_id=execution_id,
                detail={"executor": executor_name, "timeout_seconds": timeout_seconds},
            )
        return execution_id

    @staticmethod
    def _reservation_guards(
        connection: sqlite3.Connection, run_id: str, worker_id: str, moment: datetime
    ) -> list[str]:
        """예약 transaction 안에서 실행 근거를 확인하고 실패 항목을 돌려줍니다."""

        failed: list[str] = []
        run = connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            return ["run_exists"]
        if RunStatus(run["status"]).is_terminal:
            failed.append("run_active")
        if run["workspace_status"] != WorkspaceStatus.READY.value:
            failed.append("workspace_ready")

        task = connection.execute(
            "SELECT approved, is_current FROM tasks WHERE fingerprint = ?",
            (run["fingerprint"],),
        ).fetchone()
        if task is None or not task["approved"] or not task["is_current"]:
            failed.append("task_approved")

        claim = connection.execute(
            "SELECT * FROM claims WHERE claim_id = ?", (run["claim_id"],)
        ).fetchone()
        if claim is None or claim["released_at"] is not None:
            failed.append("claim_active")
        else:
            if claim["lease_owner"] != worker_id:
                failed.append("claim_owner_matches")
            if from_iso(claim["lease_expires_at"]) <= moment:
                failed.append("lease_valid")
        return failed

    def attach_process(
        self,
        execution_id: str,
        *,
        pid: int,
        identity: dict[str, Any],
        started_at: str,
        now: datetime | None = None,
    ) -> sqlite3.Row:
        """spawn된 process의 pid와 identity를 붙이고 `Running`으로 확정합니다."""

        moment = to_iso(now or utcnow())
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                raise ExecutionError("execution_not_found", f"{execution_id}를 찾을 수 없습니다.")
            if row["status"] != ExecutionStatus.STARTING.value:
                raise ExecutionConflict(
                    "execution_not_starting",
                    f"{execution_id}가 Starting 상태가 아닙니다.",
                    self._execution_from_row(row),
                )
            connection.execute(
                "UPDATE executions SET status = ?, process_id = ?, process_identity = ?, "
                "process_started_at = ?, updated_at = ? WHERE execution_id = ?",
                (
                    ExecutionStatus.RUNNING.value,
                    pid,
                    json.dumps(identity, ensure_ascii=False),
                    started_at,
                    moment,
                    execution_id,
                ),
            )
            self._record(
                connection,
                kind="execution_running",
                moment=moment,
                task_id=row["task_id"],
                run_id=row["run_id"],
                execution_id=execution_id,
                detail={"pid": pid, "identity_method": identity.get("method")},
            )
            return connection.execute(
                "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()

    def finish_execution(
        self,
        execution_id: str,
        *,
        status: ExecutionStatus,
        exit_code: int | None = None,
        finished_at: str | None = None,
        stdout: dict[str, Any] | None = None,
        stderr: dict[str, Any] | None = None,
        failure_category: str | None = None,
        error: dict[str, Any] | None = None,
        cancellation_state: str | None = None,
        now: datetime | None = None,
    ) -> sqlite3.Row:
        """execution을 terminal 상태로 기록합니다. `error`는 redaction된 값이어야 합니다."""

        moment = to_iso(now or utcnow())
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                raise ExecutionError("execution_not_found", f"{execution_id}를 찾을 수 없습니다.")
            connection.execute(
                "UPDATE executions SET status = ?, process_exit_code = ?, "
                "process_finished_at = ?, stdout_path = ?, stdout_bytes = ?, "
                "stdout_truncated = ?, stderr_path = ?, stderr_bytes = ?, "
                "stderr_truncated = ?, failure_category = ?, executor_error = ?, "
                "cancellation_state = COALESCE(?, cancellation_state), updated_at = ? "
                "WHERE execution_id = ?",
                (
                    status.value,
                    exit_code,
                    finished_at or moment,
                    (stdout or {}).get("path"),
                    (stdout or {}).get("bytes_written"),
                    1 if (stdout or {}).get("truncated") else 0,
                    (stderr or {}).get("path"),
                    (stderr or {}).get("bytes_written"),
                    1 if (stderr or {}).get("truncated") else 0,
                    failure_category,
                    json.dumps(error, ensure_ascii=False, default=str) if error else None,
                    cancellation_state,
                    moment,
                    execution_id,
                ),
            )
            self._record(
                connection,
                kind="execution_finished",
                moment=moment,
                task_id=row["task_id"],
                run_id=row["run_id"],
                execution_id=execution_id,
                detail={
                    "status": status.value,
                    "exit_code": exit_code,
                    "failure_category": failure_category,
                    "stdout_bytes": (stdout or {}).get("bytes_written"),
                    "stderr_bytes": (stderr or {}).get("bytes_written"),
                },
            )
            return connection.execute(
                "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()

    def set_cancellation_state(
        self,
        execution_id: str,
        state: str,
        *,
        reason: str = "",
        status: ExecutionStatus | None = None,
        now: datetime | None = None,
    ) -> None:
        moment = to_iso(now or utcnow())
        with self._write() as connection:
            row = connection.execute(
                "SELECT task_id, run_id FROM executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                return
            if status is None:
                connection.execute(
                    "UPDATE executions SET cancellation_state = ?, updated_at = ? "
                    "WHERE execution_id = ?",
                    (state, moment, execution_id),
                )
            else:
                connection.execute(
                    "UPDATE executions SET cancellation_state = ?, status = ?, updated_at = ? "
                    "WHERE execution_id = ?",
                    (state, status.value, moment, execution_id),
                )
            self._record(
                connection,
                kind="execution_cancellation",
                moment=moment,
                task_id=row["task_id"],
                run_id=row["run_id"],
                execution_id=execution_id,
                detail={"cancellation_state": state, "reason": reason},
            )

    def record_execution_event(
        self,
        execution_id: str | None,
        run_id: str | None,
        kind: str,
        detail: dict[str, Any],
        now: datetime | None = None,
    ) -> None:
        moment = to_iso(now or utcnow())
        with self._write() as connection:
            task_id = None
            if run_id:
                row = connection.execute(
                    "SELECT task_id FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                task_id = row["task_id"] if row else None
            self._record(
                connection,
                kind=kind,
                moment=moment,
                task_id=task_id,
                run_id=run_id,
                execution_id=execution_id,
                detail=detail,
            )

    # -- execution reads -------------------------------------------------

    def execution(self, execution_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()

    def active_execution(self, run_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM executions WHERE run_id = ? AND status IN "
            "('Starting','Running','Cancelling')",
            (run_id,),
        ).fetchone()

    def executions(self, run_id: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        query = "SELECT * FROM executions"
        params: list[Any] = []
        if run_id is not None:
            query += " WHERE run_id = ?"
            params.append(run_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return list(self._connection.execute(query, params))

    def active_executions(self) -> list[sqlite3.Row]:
        return list(
            self._connection.execute(
                "SELECT * FROM executions WHERE status IN "
                "('Starting','Running','Cancelling') ORDER BY created_at ASC"
            )
        )

    def executions_for_terminal_runs(self) -> list[sqlite3.Row]:
        """Run은 끝났는데 execution이 아직 active인 경우를 찾습니다."""

        return list(
            self._connection.execute(
                "SELECT e.* FROM executions e JOIN runs r ON r.run_id = e.run_id "
                "WHERE e.status IN ('Starting','Running','Cancelling') "
                "AND r.status NOT IN ('Pending','Running') ORDER BY e.created_at ASC"
            )
        )

    @staticmethod
    def _execution_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    # -- run reads -------------------------------------------------------



    # -- validation -------------------------------------------------------

    def start_validation(
        self,
        run_id: str,
        *,
        worker_id: str,
        cwd: str,
        plan: dict[str, Any],
        now: datetime | None = None,
    ) -> str:
        """validation을 예약하고 Run을 `Validating`으로 전이합니다.

        `AwaitingValidation`에서만 시작할 수 있습니다. 구현이 끝나지 않았거나
        이미 검증된 Run을 다시 검증하면 근거 없는 결론이 나옵니다.

        예약과 상태 전이와 근거 확인을 **하나의 transaction**에서 합니다. 첫
        확인과 예약 사이에 승인이 회수되거나 claim이 풀리는 창을 닫습니다.
        subprocess는 이 transaction 밖에서 띄웁니다.
        """

        moment = now or utcnow()
        stamp = to_iso(moment)
        validation_id = f"val-{uuid.uuid4().hex[:16]}"

        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunError("run_not_found", f"{run_id}를 찾을 수 없습니다.")

            status = RunStatus(row["status"])
            if status is not RunStatus.AWAITING_VALIDATION:
                raise ValidationConflict(
                    "run_not_awaiting_validation",
                    f"{run_id}는 {status.value} 상태입니다. "
                    f"{RunStatus.AWAITING_VALIDATION.value}에서만 시작할 수 있습니다.",
                )

            existing = connection.execute(
                "SELECT * FROM validations WHERE run_id = ? AND status IN "
                "('Starting','Running','RecoveryRequired')",
                (run_id,),
            ).fetchone()
            if existing is not None:
                raise ValidationConflict(
                    "validation_already_active",
                    f"{run_id}에 이미 active validation "
                    f"{existing['validation_id']}이 있습니다.",
                    self._validation_from_row(existing),
                )

            active_execution = connection.execute(
                "SELECT execution_id FROM executions WHERE run_id = ? AND status IN "
                "('Starting','Running','Cancelling')",
                (run_id,),
            ).fetchone()
            if active_execution is not None:
                raise ValidationConflict(
                    "execution_still_active",
                    f"{run_id}에 아직 실행 중인 execution "
                    f"{active_execution['execution_id']}이 있습니다.",
                )

            failed = self._reservation_guards(connection, run_id, worker_id, moment)
            if failed:
                raise ValidationConflict(
                    "validation_guard_failed",
                    f"validation 시작 근거 확인에 실패했습니다: {', '.join(failed)}",
                    {"failed_checks": failed},
                )

            connection.execute(
                "INSERT INTO validations(validation_id, run_id, task_id, worker_id, status, "
                "cwd, plan_json, started_at, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    validation_id,
                    run_id,
                    row["task_id"],
                    worker_id,
                    ValidationStatus.STARTING.value,
                    cwd,
                    json.dumps(plan, ensure_ascii=False),
                    stamp,
                    stamp,
                    stamp,
                ),
            )
            connection.execute(
                "UPDATE runs SET status = ?, heartbeat_at = ? WHERE run_id = ?",
                (RunStatus.VALIDATING.value, stamp, run_id),
            )
            self._record(
                connection,
                kind="validation_started",
                moment=stamp,
                task_id=row["task_id"],
                fingerprint=row["fingerprint"],
                claim_id=row["claim_id"],
                run_id=run_id,
                validation_id=validation_id,
                detail={
                    "worker_id": worker_id,
                    "ecosystem": plan.get("ecosystem"),
                    "step_count": len(plan.get("steps") or []),
                },
            )
        return validation_id

    def mark_validation_running(
        self, validation_id: str, now: datetime | None = None
    ) -> None:
        """첫 step을 시작했음을 기록합니다."""

        stamp = to_iso(now or utcnow())
        with self._write() as connection:
            connection.execute(
                "UPDATE validations SET status = ?, updated_at = ? "
                "WHERE validation_id = ? AND status = ?",
                (
                    ValidationStatus.RUNNING.value,
                    stamp,
                    validation_id,
                    ValidationStatus.STARTING.value,
                ),
            )

    def record_validation_step(
        self,
        validation_id: str,
        run_id: str,
        *,
        position: int,
        name: str,
        kind: str,
        required: bool,
        status: str,
        command: list[str] | tuple[str, ...] = (),
        exit_code: int | None = None,
        duration_seconds: float | None = None,
        process_id: int | None = None,
        process_identity: dict[str, Any] | None = None,
        process_started_at: str | None = None,
        stdout: dict[str, Any] | None = None,
        stderr: dict[str, Any] | None = None,
        reason: str = "",
        evidence: dict[str, Any] | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """step 하나의 상태를 기록합니다. 같은 position이면 갱신합니다.

        step을 시작할 때 먼저 `running`으로 기록해야 restart 후 "어디까지
        갔는지"를 알 수 있습니다. 결과만 기록하면 중간에 죽은 경우를 구분할
        수 없습니다.
        """

        stamp = to_iso(now or utcnow())
        step_id = f"vstep-{validation_id[4:]}-{position:02d}"
        with self._write() as connection:
            connection.execute(
                "INSERT INTO validation_steps(step_id, validation_id, run_id, position, name, "
                "kind, required, status, command, exit_code, duration_seconds, process_id, "
                "process_identity, process_started_at, stdout_path, stdout_bytes, "
                "stdout_truncated, stderr_path, stderr_bytes, stderr_truncated, reason, "
                "evidence, started_at, finished_at, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(step_id) DO UPDATE SET "
                "status = excluded.status, exit_code = excluded.exit_code, "
                "duration_seconds = excluded.duration_seconds, "
                "process_id = COALESCE(excluded.process_id, validation_steps.process_id), "
                "process_identity = COALESCE(excluded.process_identity, "
                "validation_steps.process_identity), "
                "process_started_at = COALESCE(excluded.process_started_at, "
                "validation_steps.process_started_at), "
                "stdout_path = excluded.stdout_path, stdout_bytes = excluded.stdout_bytes, "
                "stdout_truncated = excluded.stdout_truncated, "
                "stderr_path = excluded.stderr_path, stderr_bytes = excluded.stderr_bytes, "
                "stderr_truncated = excluded.stderr_truncated, reason = excluded.reason, "
                "evidence = excluded.evidence, finished_at = excluded.finished_at, "
                "updated_at = excluded.updated_at",
                (
                    step_id,
                    validation_id,
                    run_id,
                    position,
                    name,
                    kind,
                    1 if required else 0,
                    status,
                    json.dumps(list(command), ensure_ascii=False),
                    exit_code,
                    duration_seconds,
                    process_id,
                    json.dumps(process_identity, ensure_ascii=False)
                    if process_identity
                    else None,
                    process_started_at,
                    (stdout or {}).get("path"),
                    (stdout or {}).get("bytes_written"),
                    1 if (stdout or {}).get("truncated") else 0,
                    (stderr or {}).get("path"),
                    (stderr or {}).get("bytes_written"),
                    1 if (stderr or {}).get("truncated") else 0,
                    reason,
                    json.dumps(evidence or {}, ensure_ascii=False),
                    started_at or stamp,
                    finished_at,
                    stamp,
                    stamp,
                ),
            )
        return step_id

    def finish_validation(
        self,
        validation_id: str,
        *,
        status: ValidationStatus,
        outcome: str | None = None,
        summary: str = "",
        warnings: list[str] | tuple[str, ...] = (),
        failure_category: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """validation attempt를 종료합니다. Run 전이는 별도로 수행합니다."""

        stamp = to_iso(now or utcnow())
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM validations WHERE validation_id = ?", (validation_id,)
            ).fetchone()
            if row is None:
                raise ValidationConflict(
                    "validation_not_found", f"{validation_id}를 찾을 수 없습니다."
                )
            connection.execute(
                "UPDATE validations SET status = ?, outcome = ?, summary = ?, warnings = ?, "
                "failure_category = ?, finished_at = ?, updated_at = ? WHERE validation_id = ?",
                (
                    status.value,
                    outcome,
                    summary,
                    json.dumps(list(warnings), ensure_ascii=False),
                    failure_category,
                    stamp,
                    stamp,
                    validation_id,
                ),
            )
            self._record(
                connection,
                kind="validation_finished",
                moment=stamp,
                task_id=row["task_id"],
                run_id=row["run_id"],
                validation_id=validation_id,
                detail={
                    "status": status.value,
                    "outcome": outcome,
                    "failure_category": failure_category,
                    "warnings": list(warnings),
                },
            )

    def record_validation_event(
        self,
        validation_id: str | None,
        run_id: str,
        kind: str,
        detail: dict[str, Any],
        now: datetime | None = None,
    ) -> None:
        stamp = to_iso(now or utcnow())
        with self._write() as connection:
            row = connection.execute(
                "SELECT task_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            self._record(
                connection,
                kind=kind,
                moment=stamp,
                task_id=row["task_id"] if row else None,
                run_id=run_id,
                validation_id=validation_id,
                detail=detail,
            )

    def validation(self, validation_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM validations WHERE validation_id = ?", (validation_id,)
        ).fetchone()

    def active_validation(self, run_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM validations WHERE run_id = ? AND status IN "
            "('Starting','Running','RecoveryRequired')",
            (run_id,),
        ).fetchone()

    def validations(self, run_id: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if run_id is None:
            return self._connection.execute(
                "SELECT * FROM validations ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return self._connection.execute(
            "SELECT * FROM validations WHERE run_id = ? ORDER BY created_at DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()

    def active_validations(self) -> list[sqlite3.Row]:
        return self._connection.execute(
            "SELECT * FROM validations WHERE status IN "
            "('Starting','Running','RecoveryRequired') ORDER BY created_at ASC"
        ).fetchall()

    def validation_steps_with_live_process(self) -> list[sqlite3.Row]:
        """process가 아직 살아 있을 수 있는 step 전부.

        **validation status만 보면 놓칩니다.** 결과를 terminal로 닫은 뒤에도
        종료를 확인하지 못한 process가 남아 있을 수 있습니다. step 자체를
        기준으로 훑어야 감사에서 사라지지 않습니다.
        """

        return self._connection.execute(
            "SELECT s.*, v.status AS validation_status FROM validation_steps s "
            "JOIN validations v ON v.validation_id = s.validation_id "
            "WHERE s.process_id IS NOT NULL AND s.status IN ('running','unconfirmed') "
            "ORDER BY s.updated_at ASC"
        ).fetchall()

    def validations_for_terminal_runs(self) -> list[sqlite3.Row]:
        """terminal Run인데 validation이 아직 active한 경우입니다."""

        return self._connection.execute(
            "SELECT v.* FROM validations v JOIN runs r ON r.run_id = v.run_id "
            "WHERE v.status IN ('Starting','Running','RecoveryRequired') AND r.status IN "
            "('Succeeded','Failed','Cancelled','Orphaned') ORDER BY v.created_at ASC"
        ).fetchall()

    def validation_steps(self, validation_id: str) -> list[sqlite3.Row]:
        return self._connection.execute(
            "SELECT * FROM validation_steps WHERE validation_id = ? ORDER BY position ASC",
            (validation_id,),
        ).fetchall()

    def running_validation_step(self, validation_id: str) -> sqlite3.Row | None:
        """process가 살아 있을 수 있는 step. reconciliation이 이것을 봅니다."""

        return self._connection.execute(
            "SELECT * FROM validation_steps WHERE validation_id = ? AND status IN "
            "('running','unconfirmed') ORDER BY position ASC LIMIT 1",
            (validation_id,),
        ).fetchone()

    @staticmethod
    def _validation_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "validation_id": row["validation_id"],
            "run_id": row["run_id"],
            "status": row["status"],
            "outcome": row["outcome"],
            "worker_id": row["worker_id"],
        }

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
            workspace_status=WorkspaceStatus(row["workspace_status"]),
            branch=row["branch"],
            worktree_path=row["worktree_path"],
            base_branch=row["base_branch"],
            base_revision=row["base_revision"],
            workspace_created_at=row["workspace_created_at"],
            workspace_removed_at=row["workspace_removed_at"],
            workspace_error=row["workspace_error"],
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
        execution_id: str | None = None,
        validation_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events"
            "(occurred_at, kind, task_id, fingerprint, claim_id, run_id, execution_id, "
            "validation_id, detail) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                moment,
                kind,
                task_id,
                fingerprint,
                claim_id,
                run_id,
                execution_id,
                validation_id,
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
