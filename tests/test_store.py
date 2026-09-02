"""Persistence, idempotent registration, atomic claim, lease 테스트."""

import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from atlas.intake import IssueIntake, build_idempotency_key
from atlas.parser import parse_issue_body
from atlas.store import TaskStore, utcnow
from atlas.validation import validate_intake
from tests.fixtures import VALID_BODY, FakeIssueSource, body_replacing, make_issue


def intake_of(issue):
    key = build_idempotency_key(issue)
    return validate_intake(issue, parse_issue_body(issue.body), key), key


class StoreTestCase(unittest.TestCase):
    """파일 기반 store를 쓰는 공통 베이스. 재시작 검증에 파일이 필요합니다."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._dir.name) / "atlas.db")
        self.store = TaskStore(self.path)
        self.addCleanup(self._teardown)

    def _teardown(self):
        self.store.close()
        self._dir.cleanup()

    def register(self, issue=None, **kwargs):
        issue = issue if issue is not None else make_issue()
        result, key = intake_of(issue)
        return self.store.register(
            result,
            key,
            repository=issue.repository,
            issue_number=issue.number,
            labels=issue.labels,
            **kwargs,
        )


class RegistrationTest(StoreTestCase):
    def test_valid_issue_is_registered(self):
        outcome = self.register()

        self.assertEqual(outcome.action, "registered")
        self.assertEqual(outcome.task_id, "ATLAS-0042")
        self.assertEqual(len(self.store.current_tasks()), 1)

    def test_invalid_issue_is_refused_by_the_store(self):
        issue = make_issue(body="")
        result, key = intake_of(issue)

        with self.assertRaises(ValueError):
            self.store.register(
                result, key, repository=issue.repository, issue_number=issue.number
            )
        self.assertEqual(len(self.store.current_tasks()), 0)

    def test_repeated_registration_does_not_duplicate(self):
        first = self.register()
        second = self.register()
        third = self.register()

        self.assertEqual(first.action, "registered")
        self.assertEqual(second.action, "unchanged")
        self.assertEqual(third.action, "unchanged")
        self.assertEqual(len(self.store.current_tasks()), 1)

    def test_stored_task_json_round_trips(self):
        outcome = self.register()
        row = self.store.task_by_fingerprint(outcome.fingerprint)

        self.assertEqual(row["status"], "Draft")
        self.assertEqual(row["issue_number"], 42)
        self.assertIn("ATLAS-0042", row["task_json"])

    def test_labels_are_persisted(self):
        outcome = self.register(make_issue(labels=("atlas:queued", "docs")))
        row = self.store.task_by_fingerprint(outcome.fingerprint)

        self.assertIn("atlas:queued", row["labels"])


class RevisionTest(StoreTestCase):
    def test_edited_issue_creates_a_new_current_revision(self):
        first = self.register()
        edited = make_issue(body=body_replacing("Priority", "high"))
        second = self.register(edited)

        self.assertEqual(second.action, "revised")
        self.assertEqual(second.previous_fingerprint, first.fingerprint)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

        current = self.store.current_tasks()
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["fingerprint"], second.fingerprint)
        self.assertEqual(len(self.store.revisions("ATLAS-0042")), 2)

    def test_superseded_revision_is_retained_for_audit(self):
        first = self.register()
        self.register(make_issue(body=body_replacing("Priority", "high")))

        row = self.store.task_by_fingerprint(first.fingerprint)
        self.assertEqual(row["is_current"], 0)
        self.assertIsNotNone(row["superseded_at"])

    def test_new_revision_releases_the_existing_claim(self):
        """Issue가 수정되면 기존 승인과 claim을 자동 재사용하지 않습니다."""

        self.register()
        claim = self.store.claim("worker-a", 900)
        self.assertIsNotNone(claim)

        self.register(make_issue(body=body_replacing("Priority", "high")))

        self.assertIsNone(self.store.active_claim("ATLAS-0042"))
        kinds = [row["kind"] for row in self.store.events()]
        self.assertIn("claim_released", kinds)

    def test_reverting_to_a_previous_revision_restores_it_as_current(self):
        first = self.register()
        self.register(make_issue(body=body_replacing("Priority", "high")))
        restored = self.register()

        self.assertEqual(restored.action, "revised")
        self.assertEqual(restored.fingerprint, first.fingerprint)
        current = self.store.current_tasks()
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["fingerprint"], first.fingerprint)


class DurabilityTest(StoreTestCase):
    def test_state_survives_store_reopen(self):
        outcome = self.register()
        claim = self.store.claim("worker-a", 900)
        self.store.close()

        reopened = TaskStore(self.path)
        self.addCleanup(reopened.close)

        rows = reopened.current_tasks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fingerprint"], outcome.fingerprint)

        active = reopened.active_claim("ATLAS-0042")
        self.assertIsNotNone(active)
        self.assertEqual(active["claim_id"], claim.claim_id)
        self.assertEqual(active["lease_owner"], "worker-a")

    def test_cursor_survives_store_reopen(self):
        self.store.save_cursor("hongwon1031/atlas", "2026-09-01T10:00:00Z")
        self.store.close()

        reopened = TaskStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.cursor("hongwon1031/atlas"), "2026-09-01T10:00:00Z")

    def test_idempotency_holds_across_reopen(self):
        """persistence가 있으므로 process를 다시 열어도 중복 생성되지 않습니다."""

        self.register()
        self.store.close()

        reopened = TaskStore(self.path)
        self.addCleanup(reopened.close)
        issue = make_issue()
        result, key = intake_of(issue)
        outcome = reopened.register(
            result, key, repository=issue.repository, issue_number=issue.number
        )

        self.assertEqual(outcome.action, "unchanged")
        self.assertEqual(len(reopened.current_tasks()), 1)


class ClaimTest(StoreTestCase):
    def test_claim_records_lease_fields(self):
        self.register()
        claim = self.store.claim("worker-a", 900)

        self.assertEqual(claim.task_id, "ATLAS-0042")
        self.assertEqual(claim.claimed_by, "worker-a")
        self.assertEqual(claim.lease_owner, "worker-a")
        self.assertTrue(claim.claim_id.startswith("claim-"))
        self.assertTrue(claim.lease_expires_at)

    def test_no_task_means_no_claim(self):
        self.assertIsNone(self.store.claim("worker-a", 900))

    def test_repeated_claim_while_lease_is_active_fails(self):
        self.register()

        first = self.store.claim("worker-a", 900)
        second = self.store.claim("worker-b", 900)

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_same_worker_cannot_double_claim(self):
        self.register()
        self.store.claim("worker-a", 900)

        self.assertIsNone(self.store.claim("worker-a", 900))

    def test_released_claim_can_be_reclaimed(self):
        self.register()
        first = self.store.claim("worker-a", 900)
        self.store.release(first.claim_id, "done")

        second = self.store.claim("worker-b", 900)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.claim_id, second.claim_id)

    def test_higher_priority_task_is_claimed_first(self):
        self.register(make_issue(number=1))
        self.register(make_issue(number=2, body=body_replacing("Priority", "urgent")))

        claim = self.store.claim("worker-a", 900)
        self.assertEqual(claim.task_id, "ATLAS-0002")

    def test_claim_can_target_a_specific_task(self):
        self.register(make_issue(number=1))
        self.register(make_issue(number=2))

        claim = self.store.claim("worker-a", 900, task_id="ATLAS-0001")
        self.assertEqual(claim.task_id, "ATLAS-0001")

    def test_release_of_unknown_claim_is_false(self):
        self.assertFalse(self.store.release("claim-nope", "x"))


class LeaseTest(StoreTestCase):
    def test_active_lease_blocks_other_workers(self):
        self.register()
        self.store.claim("worker-a", lease_ttl_seconds=900)

        self.assertIsNone(self.store.claim("worker-b", 900))

    def test_expired_lease_allows_reclaim(self):
        self.register()
        first = self.store.claim("worker-a", lease_ttl_seconds=60)

        later = utcnow() + timedelta(seconds=61)
        second = self.store.claim("worker-b", 900, now=later)

        self.assertIsNotNone(second)
        self.assertEqual(second.lease_owner, "worker-b")
        self.assertNotEqual(first.claim_id, second.claim_id)

    def test_lease_expiry_is_recorded_with_previous_owner(self):
        self.register()
        self.store.claim("worker-a", lease_ttl_seconds=60)
        self.store.claim("worker-b", 900, now=utcnow() + timedelta(seconds=61))

        expiry_events = [row for row in self.store.events() if row["kind"] == "lease_expired"]
        self.assertEqual(len(expiry_events), 1)
        self.assertIn("worker-a", expiry_events[0]["detail"])

    def test_grace_period_delays_reclaim(self):
        self.register()
        self.store.claim("worker-a", lease_ttl_seconds=60)
        just_expired = utcnow() + timedelta(seconds=61)

        blocked = self.store.claim(
            "worker-b", 900, now=just_expired, grace_period_seconds=120
        )
        allowed = self.store.claim(
            "worker-b", 900, now=just_expired + timedelta(seconds=120), grace_period_seconds=120
        )

        self.assertIsNone(blocked)
        self.assertIsNotNone(allowed)

    def test_renew_lease_extends_an_active_claim(self):
        self.register()
        claim = self.store.claim("worker-a", lease_ttl_seconds=60)

        renewed = self.store.renew_lease(claim.claim_id, 900)

        self.assertIsNotNone(renewed)
        self.assertGreater(renewed, claim.lease_expires_at)

    def test_renew_lease_refuses_an_expired_claim(self):
        self.register()
        claim = self.store.claim("worker-a", lease_ttl_seconds=60)

        self.assertIsNone(
            self.store.renew_lease(claim.claim_id, 900, now=utcnow() + timedelta(seconds=61))
        )


class ConcurrentClaimTest(StoreTestCase):
    def test_only_one_of_many_racing_workers_wins(self):
        """같은 Task를 여러 worker가 동시에 claim하면 하나만 성공해야 합니다."""

        self.register()
        worker_count = 8
        barrier = threading.Barrier(worker_count)
        results: list[object] = []
        lock = threading.Lock()

        def attempt(index: int) -> None:
            store = TaskStore(self.path, busy_timeout_seconds=10.0)
            try:
                barrier.wait(timeout=10)
                claim = store.claim(f"worker-{index}", 900)
            finally:
                store.close()
            with lock:
                results.append(claim)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        winners = [claim for claim in results if claim is not None]
        self.assertEqual(len(results), worker_count)
        self.assertEqual(len(winners), 1)
        self.assertEqual(len({claim.claim_id for claim in winners}), 1)

    def test_active_claim_is_unique_in_the_database(self):
        self.register()
        self.store.claim("worker-a", 900)

        rows = self.store._connection.execute(
            "SELECT COUNT(*) AS n FROM claims WHERE task_id = ? AND released_at IS NULL",
            ("ATLAS-0042",),
        ).fetchone()
        self.assertEqual(rows["n"], 1)


class IntakeReuseTest(unittest.TestCase):
    def test_intake_record_does_not_refetch(self):
        source = FakeIssueSource(make_issue())
        intake = IssueIntake(source)

        result = intake.intake_record(make_issue())

        self.assertTrue(result.is_valid)
        self.assertEqual(source.calls, 0)

    def test_intake_record_enforces_repository_allowlist(self):
        result = IssueIntake(FakeIssueSource()).intake_record(
            make_issue(repository="someone/else")
        )

        self.assertFalse(result.is_valid)
        self.assertEqual({i.code for i in result.errors}, {"repository_not_allowed"})

    def test_intake_record_matches_intake_result(self):
        issue = make_issue()
        source = FakeIssueSource(issue)

        fetched = IssueIntake(source).intake(42)
        direct = IssueIntake(source).intake_record(issue)

        self.assertEqual(fetched.task, direct.task)
        self.assertEqual(fetched.idempotency_fingerprint, direct.idempotency_fingerprint)


if __name__ == "__main__":
    unittest.main()
