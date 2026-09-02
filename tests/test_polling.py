"""Polling 테스트."""

import tempfile
import unittest
from pathlib import Path

from atlas.config import PollingConfig
from atlas.intake import IssueIntake
from atlas.issue_source import IssueSourceError
from atlas.polling import IssuePoller, is_task_candidate
from atlas.store import TaskStore
from tests.fixtures import (
    FakeIssueSource,
    body_replacing,
    body_without,
    make_approved_issue,
    make_issue,
)


class PollerTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._dir.name) / "atlas.db")
        self.store = TaskStore(self.path)
        self.addCleanup(self._teardown)

    def _teardown(self):
        self.store.close()
        self._dir.cleanup()

    def poller(self, *issues, config=None, list_error=None, store=None):
        source = FakeIssueSource(*issues, list_error=list_error)
        self.source = source
        return IssuePoller(
            source, IssueIntake(source), store or self.store, config or PollingConfig()
        )


class CandidateFilterTest(unittest.TestCase):
    def test_marker_state_and_label_decide_candidacy(self):
        config = PollingConfig()

        self.assertTrue(is_task_candidate(make_approved_issue(), config))
        self.assertFalse(is_task_candidate(make_approved_issue(title="plain issue"), config))
        self.assertFalse(is_task_candidate(make_approved_issue(state="closed"), config))
        self.assertFalse(is_task_candidate(make_approved_issue(is_pull_request=True), config))

    def test_queue_label_is_required_by_default(self):
        """승인 signal이 없는 공개 Issue는 후보가 되지 않아야 합니다."""

        self.assertFalse(is_task_candidate(make_issue(), PollingConfig()))
        self.assertTrue(is_task_candidate(make_approved_issue(), PollingConfig()))

    def test_other_labels_do_not_satisfy_the_gate(self):
        self.assertFalse(is_task_candidate(make_issue(labels=("docs", "bug")), PollingConfig()))

    def test_gate_can_be_disabled_explicitly(self):
        ungated = PollingConfig(require_queue_label=False)

        self.assertTrue(is_task_candidate(make_issue(), ungated))


class PollRegistrationTest(PollerTestCase):
    def test_valid_candidate_is_registered(self):
        report = self.poller(make_approved_issue()).poll_once()

        self.assertEqual(report.scanned, 1)
        self.assertEqual(report.stored, 1)
        self.assertEqual(len(self.store.current_tasks()), 1)

    def test_invalid_candidate_is_not_registered(self):
        report = self.poller(make_approved_issue(body=body_without("Objective"))).poll_once()

        self.assertEqual(report.invalid, 1)
        self.assertEqual(report.stored, 0)
        self.assertEqual(len(self.store.current_tasks()), 0)

    def test_non_candidate_is_skipped_without_validation(self):
        report = self.poller(make_approved_issue(title="just a question")).poll_once()

        self.assertEqual(report.not_candidate, 1)
        self.assertEqual(report.invalid, 0)
        self.assertEqual(len(self.store.current_tasks()), 0)

    def test_mixed_batch_is_partitioned(self):
        report = self.poller(
            make_approved_issue(number=1),
            make_approved_issue(number=2, body=body_without("Objective")),
            make_approved_issue(number=3, title="unrelated"),
            make_approved_issue(number=4),
        ).poll_once()

        self.assertEqual(report.scanned, 4)
        self.assertEqual(report.stored, 2)
        self.assertEqual(report.invalid, 1)
        self.assertEqual(report.not_candidate, 1)
        self.assertEqual(len(self.store.current_tasks()), 2)

    def test_pull_requests_never_reach_the_store(self):
        report = self.poller(make_approved_issue(number=9, is_pull_request=True)).poll_once()

        self.assertEqual(report.stored, 0)
        self.assertEqual(len(self.store.current_tasks()), 0)

    def test_poller_does_not_refetch_listed_issues(self):
        poller = self.poller(make_approved_issue())
        poller.poll_once()

        self.assertEqual(self.source.calls, 0)


class PollIdempotencyTest(PollerTestCase):
    def test_repeated_polling_does_not_duplicate_tasks(self):
        poller = self.poller(make_approved_issue())

        first = poller.poll_once()
        second = poller.poll_once()
        third = poller.poll_once()

        self.assertEqual(first.stored, 1)
        self.assertEqual(second.stored, 0)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(third.unchanged, 1)
        self.assertEqual(len(self.store.current_tasks()), 1)

    def test_repeated_polling_across_new_poller_instances(self):
        self.poller(make_approved_issue()).poll_once()
        report = self.poller(make_approved_issue()).poll_once()

        self.assertEqual(report.unchanged, 1)
        self.assertEqual(len(self.store.current_tasks()), 1)

    def test_edited_issue_is_polled_as_a_new_revision(self):
        poller = self.poller(make_approved_issue())
        poller.poll_once()

        self.source.replace(make_approved_issue(body=body_replacing("Priority", "urgent")))
        report = poller.poll_once()

        self.assertEqual(len(report.revised), 1)
        self.assertEqual(len(self.store.revisions("ATLAS-0042")), 2)
        self.assertEqual(len(self.store.current_tasks()), 1)

    def test_issue_becoming_invalid_leaves_the_stored_revision_untouched(self):
        poller = self.poller(make_approved_issue())
        poller.poll_once()

        self.source.replace(make_approved_issue(body=body_without("Objective")))
        report = poller.poll_once()

        self.assertEqual(report.invalid, 1)
        self.assertEqual(report.stored, 0)
        self.assertEqual(len(self.store.current_tasks()), 1)


class PollCursorTest(PollerTestCase):
    def test_cursor_advances_to_the_newest_updated_at(self):
        poller = self.poller(
            make_approved_issue(number=1, updated_at="2026-09-01T01:00:00Z"),
            make_approved_issue(number=2, updated_at="2026-09-01T05:00:00Z"),
        )
        poller.poll_once()

        self.assertEqual(self.store.cursor("hongwon1031/atlas"), "2026-09-01T05:00:00Z")

    def test_saved_cursor_is_sent_on_the_next_pass(self):
        poller = self.poller(make_approved_issue(updated_at="2026-09-01T05:00:00Z"))
        poller.poll_once()
        poller.poll_once()

        self.assertEqual(self.source.list_calls, [None, "2026-09-01T05:00:00Z"])

    def test_cursor_survives_reopen(self):
        self.poller(make_approved_issue(updated_at="2026-09-01T05:00:00Z")).poll_once()
        self.store.close()

        reopened = TaskStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.cursor("hongwon1031/atlas"), "2026-09-01T05:00:00Z")


class PollErrorTest(PollerTestCase):
    def test_source_error_is_reported_not_raised(self):
        poller = self.poller(list_error=IssueSourceError("rate_limited", "limit"))

        report = poller.poll_once()

        self.assertEqual(report.error, "rate_limited")
        self.assertEqual(report.scanned, 0)
        self.assertEqual(len(self.store.current_tasks()), 0)

    def test_source_error_does_not_advance_the_cursor(self):
        self.poller(list_error=IssueSourceError("network", "down")).poll_once()

        self.assertIsNone(self.store.cursor("hongwon1031/atlas"))

    def test_error_categories_are_surfaced(self):
        for category in ("authentication", "not_found", "provider_unavailable"):
            report = self.poller(list_error=IssueSourceError(category, "x")).poll_once()
            self.assertEqual(report.error, category)


class RepositoryBoundaryTest(PollerTestCase):
    def test_repository_outside_allowlist_is_rejected_without_listing(self):
        """경계 확인 전에 다른 repository로 목록 요청을 보내면 안 됩니다."""

        poller = self.poller(
            make_approved_issue(), config=PollingConfig(repository="attacker/private")
        )
        report = poller.poll_once()

        self.assertEqual(self.source.list_calls, [])
        self.assertEqual(report.error, "repository_not_allowed")
        self.assertEqual(report.scanned, 0)
        self.assertEqual(len(self.store.current_tasks()), 0)

    def test_rejected_repository_does_not_save_a_cursor(self):
        self.poller(config=PollingConfig(repository="attacker/private")).poll_once()

        self.assertIsNone(self.store.cursor("attacker/private"))

    def test_allowed_repository_still_lists(self):
        self.poller(make_approved_issue()).poll_once()

        self.assertEqual(len(self.source.list_calls), 1)


class RetryAfterTest(PollerTestCase):
    def test_retry_after_is_surfaced_in_the_report(self):
        poller = self.poller(list_error=IssueSourceError("rate_limited", "x", retry_after=120))

        self.assertEqual(poller.poll_once().retry_after, 120)

    def test_retry_after_becomes_the_minimum_backoff_delay(self):
        """GitHub가 지정한 대기 시간을 고정 backoff이 덮어쓰면 안 됩니다."""

        config = PollingConfig(backoff_initial_seconds=5.0, backoff_max_seconds=15.0)
        poller = self.poller(
            list_error=IssueSourceError("rate_limited", "x", retry_after=120), config=config
        )
        delays: list[float] = []

        poller.run(max_iterations=2, sleep=delays.append)

        self.assertEqual(delays, [120.0])

    def test_backoff_wins_when_it_exceeds_retry_after(self):
        config = PollingConfig(backoff_initial_seconds=200.0, backoff_max_seconds=300.0)
        poller = self.poller(
            list_error=IssueSourceError("rate_limited", "x", retry_after=10), config=config
        )
        delays: list[float] = []

        poller.run(max_iterations=2, sleep=delays.append)

        self.assertEqual(delays, [200.0])

    def test_absent_retry_after_falls_back_to_backoff(self):
        config = PollingConfig(backoff_initial_seconds=5.0)
        poller = self.poller(list_error=IssueSourceError("network", "x"), config=config)
        delays: list[float] = []

        poller.run(max_iterations=2, sleep=delays.append)

        self.assertEqual(delays, [5.0])


class UnboundedWatchTest(PollerTestCase):
    def test_unbounded_run_streams_reports_without_accumulating(self):
        """무한 watch는 report를 누적하면 메모리가 계속 증가합니다."""

        poller = self.poller(make_approved_issue())
        seen: list[object] = []

        def stop_after_three(_delay: float) -> None:
            if len(seen) >= 3:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            poller.run(max_iterations=None, sleep=stop_after_three, on_report=seen.append)

        self.assertEqual(len(seen), 3)

    def test_bounded_run_still_returns_reports(self):
        poller = self.poller(make_approved_issue())

        reports = poller.run(max_iterations=2, sleep=lambda _: None)

        self.assertEqual(len(reports), 2)


class ApprovalReconciliationTest(PollerTestCase):
    """label 제거와 Issue 종료가 승인 회수로 이어져야 합니다."""

    def test_removing_the_label_revokes_approval_and_blocks_claim(self):
        poller = self.poller(make_approved_issue())
        first = poller.poll_once()
        self.assertEqual(first.stored, 1)
        self.assertIsNotNone(self.store.claim("worker-a", 900))
        self.store.release(self.store.active_claim("ATLAS-0042")["claim_id"], "test")

        self.source.replace(make_issue(labels=()))
        second = poller.poll_once()

        self.assertEqual(second.not_candidate, 1)
        self.assertEqual(second.revoked, ("ATLAS-0042",))
        self.assertIsNone(self.store.claim("worker-b", 900))

    def test_removing_the_label_releases_an_active_claim(self):
        poller = self.poller(make_approved_issue())
        poller.poll_once()
        self.store.claim("worker-a", 900)

        self.source.replace(make_issue(labels=()))
        poller.poll_once()

        self.assertIsNone(self.store.active_claim("ATLAS-0042"))

    def test_closing_the_issue_revokes_approval(self):
        poller = self.poller(make_approved_issue())
        poller.poll_once()

        self.source.replace(make_approved_issue(state="closed"))
        report = poller.poll_once()

        self.assertEqual(report.revoked, ("ATLAS-0042",))
        self.assertIsNone(self.store.claim("worker-a", 900))

    def test_issue_becoming_invalid_revokes_approval(self):
        poller = self.poller(make_approved_issue())
        poller.poll_once()

        self.source.replace(make_approved_issue(body=body_without("Objective")))
        report = poller.poll_once()

        self.assertEqual(report.invalid, 1)
        self.assertEqual(report.revoked, ("ATLAS-0042",))
        self.assertIsNone(self.store.claim("worker-a", 900))

    def test_restoring_the_label_restores_claimability(self):
        poller = self.poller(make_approved_issue())
        poller.poll_once()
        self.source.replace(make_issue(labels=()))
        poller.poll_once()

        self.source.replace(make_approved_issue())
        poller.poll_once()

        self.assertIsNotNone(self.store.claim("worker-a", 900))

    def test_revocation_is_not_repeated_on_every_pass(self):
        poller = self.poller(make_approved_issue())
        poller.poll_once()
        self.source.replace(make_issue(labels=()))

        first = poller.poll_once()
        second = poller.poll_once()

        self.assertEqual(first.revoked, ("ATLAS-0042",))
        self.assertEqual(second.revoked, ())

    def test_unknown_issue_losing_its_label_reports_nothing(self):
        poller = self.poller(make_issue(labels=()))

        report = poller.poll_once()

        self.assertEqual(report.not_candidate, 1)
        self.assertEqual(report.revoked, ())


class ApprovalGateBypassTest(PollerTestCase):
    """--no-queue-label은 등록만 허용하고 approval 정책을 우회하지 못합니다."""

    def test_gate_disabled_registers_but_does_not_approve(self):
        poller = self.poller(make_issue(), config=PollingConfig(require_queue_label=False))

        report = poller.poll_once()

        self.assertEqual(report.stored, 1)
        self.assertIsNone(self.store.claim("worker-a", 900))

    def test_gate_disabled_still_approves_labeled_issues(self):
        poller = self.poller(
            make_approved_issue(), config=PollingConfig(require_queue_label=False)
        )

        poller.poll_once()

        self.assertIsNotNone(self.store.claim("worker-a", 900))


class PollLoopTest(PollerTestCase):
    def test_run_uses_the_configured_interval_between_passes(self):
        poller = self.poller(make_approved_issue(), config=PollingConfig(interval_seconds=30.0))
        delays: list[float] = []

        poller.run(max_iterations=3, sleep=delays.append)

        self.assertEqual(delays, [30.0, 30.0])

    def test_run_applies_exponential_backoff_on_errors(self):
        config = PollingConfig(
            interval_seconds=30.0,
            backoff_initial_seconds=5.0,
            backoff_multiplier=2.0,
            backoff_max_seconds=15.0,
        )
        poller = self.poller(list_error=IssueSourceError("rate_limited", "x"), config=config)
        delays: list[float] = []

        poller.run(max_iterations=5, sleep=delays.append)

        self.assertEqual(delays, [5.0, 10.0, 15.0, 15.0])

    def test_backoff_resets_after_a_successful_pass(self):
        config = PollingConfig(interval_seconds=30.0, backoff_initial_seconds=5.0)
        self.assertEqual(config.backoff_delay(1), 5.0)
        self.assertEqual(config.backoff_delay(0), 0.0)

    def test_run_returns_one_report_per_iteration(self):
        poller = self.poller(make_approved_issue())

        reports = poller.run(max_iterations=2, sleep=lambda _: None)

        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0].stored, 1)
        self.assertEqual(reports[1].unchanged, 1)


class PollToClaimTest(PollerTestCase):
    def test_polled_task_becomes_claimable(self):
        self.poller(make_approved_issue()).poll_once()

        claim = self.store.claim("worker-a", 900)

        self.assertIsNotNone(claim)
        self.assertEqual(claim.task_id, "ATLAS-0042")

    def test_invalid_issue_is_never_claimable(self):
        self.poller(make_approved_issue(body=body_without("Objective"))).poll_once()

        self.assertIsNone(self.store.claim("worker-a", 900))


if __name__ == "__main__":
    unittest.main()
