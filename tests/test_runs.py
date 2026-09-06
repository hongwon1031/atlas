"""Run lifecycle, heartbeat, restart reconciliation 테스트."""

import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from atlas.config import RunConfig
from atlas.intake import build_idempotency_key
from atlas.parser import parse_issue_body
from atlas.reconciliation import RunReconciler
from atlas.schema import RunFailure, RunStatus
from atlas.store import RunError, TaskStore, utcnow
from atlas.validation import validate_intake
from tests.fixtures import body_replacing, make_issue

WORKER = "worker-a"


class RunTestCase(unittest.TestCase):
    """approved + claimed Task 하나를 준비한 상태에서 시작합니다."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._dir.name) / "atlas.db")
        self.store = TaskStore(self.path)
        self.addCleanup(self._teardown)
        self.register()
        self.claim = self.store.claim(WORKER, 900)

    def _teardown(self):
        self.store.close()
        self._dir.cleanup()

    def register(self, issue=None, *, approved=True, store=None):
        issue = issue if issue is not None else make_issue()
        key = build_idempotency_key(issue)
        result = validate_intake(issue, parse_issue_body(issue.body), key)
        return (store or self.store).register(
            result,
            key,
            repository=issue.repository,
            issue_number=issue.number,
            labels=issue.labels,
            approved=approved,
            approval_signal="queue_label:atlas:queued" if approved else None,
        )

    def kinds(self):
        return [row["kind"] for row in self.store.events()]


class StartRunTest(RunTestCase):
    def test_approved_and_claimed_task_starts_a_run(self):
        run = self.store.start_run("ATLAS-0042", WORKER)

        self.assertEqual(run.task_id, "ATLAS-0042")
        self.assertEqual(run.status, RunStatus.PENDING)
        self.assertEqual(run.worker_id, WORKER)
        self.assertEqual(run.claim_id, self.claim.claim_id)
        self.assertTrue(run.run_id.startswith("run-"))
        self.assertIsNone(run.previous_run_id)
        self.assertIn("run_started", self.kinds())

    def test_run_is_readable_and_becomes_the_active_run(self):
        run = self.store.start_run("ATLAS-0042", WORKER)

        self.assertEqual(self.store.run(run.run_id), run)
        self.assertEqual(self.store.active_run("ATLAS-0042"), run)
        self.assertEqual([r.run_id for r in self.store.active_runs()], [run.run_id])

    def test_task_without_claim_cannot_start_a_run(self):
        self.store.release(self.claim.claim_id, "test")

        with self.assertRaises(RunError) as caught:
            self.store.start_run("ATLAS-0042", WORKER)

        self.assertEqual(caught.exception.category, "no_active_claim")
        self.assertIsNone(self.store.active_run("ATLAS-0042"))

    def test_unapproved_task_cannot_start_a_run(self):
        store = TaskStore(str(Path(self._dir.name) / "other.db"))
        self.addCleanup(store.close)
        self.register(approved=False, store=store)

        with self.assertRaises(RunError) as caught:
            store.start_run("ATLAS-0042", WORKER)

        self.assertEqual(caught.exception.category, "task_not_approved")

    def test_unknown_task_cannot_start_a_run(self):
        with self.assertRaises(RunError) as caught:
            self.store.start_run("ATLAS-9999", WORKER)

        self.assertEqual(caught.exception.category, "task_not_found")

    def test_non_lease_owner_cannot_start_a_run(self):
        with self.assertRaises(RunError) as caught:
            self.store.start_run("ATLAS-0042", "worker-intruder")

        self.assertEqual(caught.exception.category, "worker_mismatch")

    def test_expired_lease_cannot_start_a_run(self):
        later = utcnow() + timedelta(seconds=901)

        with self.assertRaises(RunError) as caught:
            self.store.start_run("ATLAS-0042", WORKER, now=later)

        self.assertEqual(caught.exception.category, "lease_expired")

    def test_duplicate_active_run_is_refused(self):
        first = self.store.start_run("ATLAS-0042", WORKER)

        with self.assertRaises(RunError) as caught:
            self.store.start_run("ATLAS-0042", WORKER)

        self.assertEqual(caught.exception.category, "active_run_exists")
        self.assertEqual(self.store.active_run("ATLAS-0042").run_id, first.run_id)
        self.assertEqual(len(self.store.runs("ATLAS-0042")), 1)

    def test_duplicate_is_refused_even_after_the_run_is_running(self):
        first = self.store.start_run("ATLAS-0042", WORKER)
        self.store.heartbeat(first.run_id, WORKER)

        with self.assertRaises(RunError):
            self.store.start_run("ATLAS-0042", WORKER)

    def test_database_index_enforces_one_active_run(self):
        run = self.store.start_run("ATLAS-0042", WORKER)

        with self.assertRaises(sqlite3.IntegrityError):
            self.store._connection.execute(
                "INSERT INTO runs(run_id, task_id, fingerprint, claim_id, worker_id,"
                " status, created_at, heartbeat_at) VALUES (?,?,?,?,?,?,?,?)",
                ("run-forced", run.task_id, run.fingerprint, run.claim_id, WORKER,
                 "Running", run.created_at, run.created_at),
            )


class HeartbeatTest(RunTestCase):
    def setUp(self):
        super().setUp()
        self.run = self.store.start_run("ATLAS-0042", WORKER)

    def test_first_heartbeat_promotes_pending_to_running(self):
        updated = self.store.heartbeat(self.run.run_id, WORKER)

        self.assertEqual(updated.status, RunStatus.RUNNING)
        self.assertIsNotNone(updated.started_at)
        self.assertIn("run_running", self.kinds())

    def test_heartbeat_refreshes_the_timestamp(self):
        later = utcnow() + timedelta(seconds=45)

        updated = self.store.heartbeat(self.run.run_id, WORKER, now=later)

        self.assertGreater(updated.heartbeat_at, self.run.heartbeat_at)

    def test_started_at_is_not_overwritten_by_later_heartbeats(self):
        first = self.store.heartbeat(self.run.run_id, WORKER)
        second = self.store.heartbeat(
            self.run.run_id, WORKER, now=utcnow() + timedelta(seconds=60)
        )

        self.assertEqual(first.started_at, second.started_at)

    def test_wrong_worker_heartbeat_is_refused(self):
        with self.assertRaises(RunError) as caught:
            self.store.heartbeat(self.run.run_id, "worker-intruder")

        self.assertEqual(caught.exception.category, "worker_mismatch")
        self.assertEqual(self.store.run(self.run.run_id).status, RunStatus.PENDING)

    def test_unknown_run_heartbeat_is_refused(self):
        with self.assertRaises(RunError) as caught:
            self.store.heartbeat("run-nope", WORKER)

        self.assertEqual(caught.exception.category, "run_not_found")

    def test_terminal_run_heartbeat_is_refused(self):
        self.store.finish_run(self.run.run_id, RunStatus.SUCCEEDED)

        with self.assertRaises(RunError) as caught:
            self.store.heartbeat(self.run.run_id, WORKER)

        self.assertEqual(caught.exception.category, "run_terminal")

    def test_cancelled_run_heartbeat_is_refused(self):
        self.store.finish_run(self.run.run_id, RunStatus.CANCELLED)

        with self.assertRaises(RunError):
            self.store.heartbeat(self.run.run_id, WORKER)


class FinishRunTest(RunTestCase):
    def setUp(self):
        super().setUp()
        self.run = self.store.start_run("ATLAS-0042", WORKER)

    def test_success_transition(self):
        finished = self.store.finish_run(self.run.run_id, RunStatus.SUCCEEDED)

        self.assertEqual(finished.status, RunStatus.SUCCEEDED)
        self.assertIsNotNone(finished.finished_at)
        self.assertIsNone(finished.failure_category)
        self.assertIsNone(self.store.active_run("ATLAS-0042"))

    def test_failure_transition_preserves_structured_reason(self):
        finished = self.store.finish_run(
            self.run.run_id,
            RunStatus.FAILED,
            failure=RunFailure("validation_failed", "테스트가 실패했습니다."),
        )

        self.assertEqual(finished.status, RunStatus.FAILED)
        self.assertEqual(finished.failure_category, "validation_failed")
        self.assertEqual(finished.failure_message, "테스트가 실패했습니다.")

    def test_cancel_transition(self):
        finished = self.store.finish_run(
            self.run.run_id,
            RunStatus.CANCELLED,
            failure=RunFailure("cancelled_by_human", "사람이 취소했습니다."),
        )

        self.assertEqual(finished.status, RunStatus.CANCELLED)
        self.assertIsNotNone(finished.finished_at)

    def test_failure_requires_a_reason(self):
        with self.assertRaises(RunError) as caught:
            self.store.finish_run(self.run.run_id, RunStatus.FAILED)

        self.assertEqual(caught.exception.category, "missing_failure")

    def test_success_rejects_a_failure_reason(self):
        with self.assertRaises(RunError) as caught:
            self.store.finish_run(
                self.run.run_id, RunStatus.SUCCEEDED, failure=RunFailure("timeout")
            )

        self.assertEqual(caught.exception.category, "unexpected_failure")

    def test_non_terminal_status_is_refused(self):
        with self.assertRaises(RunError) as caught:
            self.store.finish_run(self.run.run_id, RunStatus.RUNNING)

        self.assertEqual(caught.exception.category, "not_terminal_status")

    def test_double_finish_is_refused(self):
        self.store.finish_run(self.run.run_id, RunStatus.SUCCEEDED)

        with self.assertRaises(RunError) as caught:
            self.store.finish_run(self.run.run_id, RunStatus.SUCCEEDED)

        self.assertEqual(caught.exception.category, "run_terminal")

    def test_wrong_worker_finish_is_refused_when_owner_is_checked(self):
        with self.assertRaises(RunError) as caught:
            self.store.finish_run(
                self.run.run_id, RunStatus.SUCCEEDED, worker_id="worker-intruder"
            )

        self.assertEqual(caught.exception.category, "worker_mismatch")

    def test_unknown_failure_category_is_refused(self):
        with self.assertRaises(ValueError):
            RunFailure("made_up_category", "x")


class RetryChainTest(RunTestCase):
    def test_retry_links_to_the_previous_run(self):
        first = self.store.start_run("ATLAS-0042", WORKER)
        self.store.finish_run(
            first.run_id, RunStatus.FAILED, failure=RunFailure("transient_executor", "일시 오류")
        )

        retry = self.store.start_run("ATLAS-0042", WORKER, previous_run_id=first.run_id)

        self.assertEqual(retry.previous_run_id, first.run_id)
        self.assertNotEqual(retry.run_id, first.run_id)
        self.assertEqual(len(self.store.runs("ATLAS-0042")), 2)

    def test_previous_run_is_preserved_not_overwritten(self):
        first = self.store.start_run("ATLAS-0042", WORKER)
        self.store.finish_run(
            first.run_id, RunStatus.FAILED, failure=RunFailure("timeout", "제한 시간 초과")
        )
        self.store.start_run("ATLAS-0042", WORKER, previous_run_id=first.run_id)

        preserved = self.store.run(first.run_id)
        self.assertEqual(preserved.status, RunStatus.FAILED)
        self.assertEqual(preserved.failure_category, "timeout")

    def test_retry_cannot_reference_an_active_run(self):
        first = self.store.start_run("ATLAS-0042", WORKER)
        self.store.finish_run(first.run_id, RunStatus.SUCCEEDED)
        second = self.store.start_run("ATLAS-0042", WORKER)
        self.store.finish_run(second.run_id, RunStatus.SUCCEEDED)
        third = self.store.start_run("ATLAS-0042", WORKER)

        # third가 아직 active이므로 previous로 지정할 수 없습니다.
        self.store.finish_run(third.run_id, RunStatus.SUCCEEDED)
        fourth = self.store.start_run("ATLAS-0042", WORKER, previous_run_id=third.run_id)
        self.assertEqual(fourth.previous_run_id, third.run_id)

    def test_unknown_previous_run_is_refused(self):
        with self.assertRaises(RunError) as caught:
            self.store.start_run("ATLAS-0042", WORKER, previous_run_id="run-nope")

        self.assertEqual(caught.exception.category, "previous_run_not_found")


class DurabilityTest(RunTestCase):
    def test_run_state_survives_store_reopen(self):
        run = self.store.start_run("ATLAS-0042", WORKER)
        self.store.heartbeat(run.run_id, WORKER)
        self.store.close()

        reopened = TaskStore(self.path)
        self.addCleanup(reopened.close)

        restored = reopened.run(run.run_id)
        self.assertEqual(restored.status, RunStatus.RUNNING)
        self.assertEqual(restored.worker_id, WORKER)
        self.assertEqual(reopened.active_run("ATLAS-0042").run_id, run.run_id)

    def test_terminal_state_and_failure_survive_reopen(self):
        run = self.store.start_run("ATLAS-0042", WORKER)
        self.store.finish_run(
            run.run_id, RunStatus.FAILED, failure=RunFailure("policy_violation", "범위 위반")
        )
        self.store.close()

        reopened = TaskStore(self.path)
        self.addCleanup(reopened.close)

        restored = reopened.run(run.run_id)
        self.assertEqual(restored.status, RunStatus.FAILED)
        self.assertEqual(restored.failure_category, "policy_violation")
        self.assertIsNone(reopened.active_run("ATLAS-0042"))

    def test_duplicate_start_is_still_refused_after_reopen(self):
        self.store.start_run("ATLAS-0042", WORKER)
        self.store.close()

        reopened = TaskStore(self.path)
        self.addCleanup(reopened.close)

        with self.assertRaises(RunError) as caught:
            reopened.start_run("ATLAS-0042", WORKER)
        self.assertEqual(caught.exception.category, "active_run_exists")


class ConcurrentStartTest(RunTestCase):
    def test_only_one_of_many_racing_workers_starts_a_run(self):
        worker_count = 8
        barrier = threading.Barrier(worker_count)
        outcomes: list[object] = []
        lock = threading.Lock()

        def attempt() -> None:
            store = TaskStore(self.path, busy_timeout_seconds=10.0)
            try:
                barrier.wait(timeout=10)
                try:
                    result = store.start_run("ATLAS-0042", WORKER)
                except (RunError, sqlite3.IntegrityError) as error:
                    result = error
            finally:
                store.close()
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=attempt) for _ in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        started = [o for o in outcomes if not isinstance(o, Exception)]
        self.assertEqual(len(outcomes), worker_count)
        self.assertEqual(len(started), 1)
        self.assertEqual(len(self.store.runs("ATLAS-0042")), 1)


class ReconciliationTest(RunTestCase):
    def setUp(self):
        super().setUp()
        self.config = RunConfig(heartbeat_interval_seconds=30.0, stale_after_seconds=300.0)
        self.reconciler = RunReconciler(self.store, self.config)
        self.run = self.store.start_run("ATLAS-0042", WORKER)
        self.store.heartbeat(self.run.run_id, WORKER)

    def test_healthy_run_is_kept(self):
        report = self.reconciler.reconcile()

        self.assertEqual(report.checked, 1)
        self.assertEqual(report.healthy, (self.run.run_id,))
        self.assertEqual(report.orphaned, ())
        self.assertEqual(self.store.run(self.run.run_id).status, RunStatus.RUNNING)

    def test_stale_run_becomes_orphaned(self):
        later = utcnow() + timedelta(seconds=301)

        report = self.reconciler.reconcile(now=later)

        self.assertEqual(report.orphaned, (self.run.run_id,))
        restored = self.store.run(self.run.run_id)
        self.assertEqual(restored.status, RunStatus.ORPHANED)
        self.assertEqual(restored.failure_category, "worker_lost")
        self.assertIsNotNone(restored.finished_at)

    def test_orphaning_records_the_evidence(self):
        self.reconciler.reconcile(now=utcnow() + timedelta(seconds=301))

        orphan_events = [row for row in self.store.events() if row["kind"] == "run_orphaned"]
        self.assertEqual(len(orphan_events), 1)
        detail = orphan_events[0]["detail"]
        self.assertIn("heartbeat_age_seconds", detail)
        self.assertIn("stale_after_seconds", detail)
        self.assertIn("process_identity_checked", detail)

    def test_stale_run_is_not_restarted_automatically(self):
        """stale Run을 무조건 재실행하면 안 됩니다."""

        self.reconciler.reconcile(now=utcnow() + timedelta(seconds=301))

        self.assertIsNone(self.store.active_run("ATLAS-0042"))
        self.assertEqual(len(self.store.runs("ATLAS-0042")), 1)

    def test_orphaned_run_can_be_retried_explicitly(self):
        later = utcnow() + timedelta(seconds=301)
        self.reconciler.reconcile(now=later)

        retry = self.store.start_run(
            "ATLAS-0042", WORKER, previous_run_id=self.run.run_id, now=later
        )

        self.assertEqual(retry.previous_run_id, self.run.run_id)
        self.assertEqual(self.store.run(self.run.run_id).status, RunStatus.ORPHANED)

    def test_verdict_reports_released_claim(self):
        self.store.release(self.claim.claim_id, "worker gone")

        verdict = self.reconciler.evaluate(
            self.store.run(self.run.run_id), now=utcnow() + timedelta(seconds=301)
        )

        self.assertEqual(verdict.action, "orphan")
        self.assertTrue(verdict.evidence["claim_released"])

    def test_verdict_reports_expired_lease(self):
        verdict = self.reconciler.evaluate(
            self.store.run(self.run.run_id), now=utcnow() + timedelta(seconds=901)
        )

        self.assertEqual(verdict.action, "orphan")
        self.assertTrue(verdict.evidence["lease_expired"])

    def test_evaluate_does_not_change_state(self):
        self.reconciler.evaluate(
            self.store.run(self.run.run_id), now=utcnow() + timedelta(seconds=301)
        )

        self.assertEqual(self.store.run(self.run.run_id).status, RunStatus.RUNNING)

    def test_pending_run_that_never_started_is_also_reconciled(self):
        second = TaskStore(str(Path(self._dir.name) / "pending.db"))
        self.addCleanup(second.close)
        self.register(store=second)
        second.claim(WORKER, 900)
        pending = second.start_run("ATLAS-0042", WORKER)

        report = RunReconciler(second, self.config).reconcile(
            now=utcnow() + timedelta(seconds=301)
        )

        self.assertEqual(report.orphaned, (pending.run_id,))

    def test_terminal_runs_are_not_checked(self):
        self.store.finish_run(self.run.run_id, RunStatus.SUCCEEDED)

        report = self.reconciler.reconcile(now=utcnow() + timedelta(seconds=900))

        self.assertEqual(report.checked, 0)

    def test_reconcile_is_idempotent(self):
        later = utcnow() + timedelta(seconds=301)

        first = self.reconciler.reconcile(now=later)
        second = self.reconciler.reconcile(now=later)

        self.assertEqual(len(first.orphaned), 1)
        self.assertEqual(second.checked, 0)

    def test_reconcile_survives_restart(self):
        """worker crash 시뮬레이션: heartbeat 없이 프로세스가 사라진 상황."""

        self.store.close()
        later = utcnow() + timedelta(seconds=301)

        restarted = TaskStore(self.path)
        self.addCleanup(restarted.close)
        report = RunReconciler(restarted, self.config).reconcile(now=later)

        self.assertEqual(report.orphaned, (self.run.run_id,))
        self.assertEqual(restarted.run(self.run.run_id).status, RunStatus.ORPHANED)


class RunConfigTest(unittest.TestCase):
    def test_defaults_are_sane(self):
        config = RunConfig()

        self.assertGreater(config.stale_after_seconds, config.heartbeat_interval_seconds)

    def test_stale_threshold_must_exceed_the_interval(self):
        with self.assertRaises(ValueError):
            RunConfig(heartbeat_interval_seconds=60.0, stale_after_seconds=60.0)

    def test_non_positive_values_are_rejected(self):
        with self.assertRaises(ValueError):
            RunConfig(heartbeat_interval_seconds=0)
        with self.assertRaises(ValueError):
            RunConfig(stale_after_seconds=-1)


class MultipleTaskTest(RunTestCase):
    def test_runs_are_isolated_per_task(self):
        other = make_issue(number=99, body=body_replacing("Priority", "high"))
        self.register(other)
        second_claim = self.store.claim(WORKER, 900, task_id="ATLAS-0099")
        self.assertIsNotNone(second_claim)

        first = self.store.start_run("ATLAS-0042", WORKER)
        second = self.store.start_run("ATLAS-0099", WORKER)

        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(len(self.store.active_runs()), 2)
        self.assertEqual(len(self.store.runs("ATLAS-0042")), 1)


if __name__ == "__main__":
    unittest.main()
