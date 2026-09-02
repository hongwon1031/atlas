"""Polling 테스트."""

import tempfile
import unittest
from pathlib import Path

from atlas.config import PollingConfig
from atlas.intake import IssueIntake
from atlas.issue_source import IssueSourceError
from atlas.polling import IssuePoller, is_task_candidate
from atlas.store import TaskStore
from tests.fixtures import FakeIssueSource, body_replacing, body_without, make_issue


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
    def test_marker_and_state_decide_candidacy(self):
        config = PollingConfig()

        self.assertTrue(is_task_candidate(make_issue(), config))
        self.assertFalse(is_task_candidate(make_issue(title="plain issue"), config))
        self.assertFalse(is_task_candidate(make_issue(state="closed"), config))
        self.assertFalse(is_task_candidate(make_issue(is_pull_request=True), config))

    def test_queue_label_gating_is_opt_in(self):
        gated = PollingConfig(require_queue_label=True)

        self.assertFalse(is_task_candidate(make_issue(), gated))
        self.assertTrue(is_task_candidate(make_issue(labels=("atlas:queued",)), gated))
        self.assertTrue(is_task_candidate(make_issue(), PollingConfig()))


class PollRegistrationTest(PollerTestCase):
    def test_valid_candidate_is_registered(self):
        report = self.poller(make_issue()).poll_once()

        self.assertEqual(report.scanned, 1)
        self.assertEqual(report.stored, 1)
        self.assertEqual(len(self.store.current_tasks()), 1)

    def test_invalid_candidate_is_not_registered(self):
        report = self.poller(make_issue(body=body_without("Objective"))).poll_once()

        self.assertEqual(report.invalid, 1)
        self.assertEqual(report.stored, 0)
        self.assertEqual(len(self.store.current_tasks()), 0)

    def test_non_candidate_is_skipped_without_validation(self):
        report = self.poller(make_issue(title="just a question")).poll_once()

        self.assertEqual(report.not_candidate, 1)
        self.assertEqual(report.invalid, 0)
        self.assertEqual(len(self.store.current_tasks()), 0)

    def test_mixed_batch_is_partitioned(self):
        report = self.poller(
            make_issue(number=1),
            make_issue(number=2, body=body_without("Objective")),
            make_issue(number=3, title="unrelated"),
            make_issue(number=4),
        ).poll_once()

        self.assertEqual(report.scanned, 4)
        self.assertEqual(report.stored, 2)
        self.assertEqual(report.invalid, 1)
        self.assertEqual(report.not_candidate, 1)
        self.assertEqual(len(self.store.current_tasks()), 2)

    def test_pull_requests_never_reach_the_store(self):
        report = self.poller(make_issue(number=9, is_pull_request=True)).poll_once()

        self.assertEqual(report.stored, 0)
        self.assertEqual(len(self.store.current_tasks()), 0)

    def test_poller_does_not_refetch_listed_issues(self):
        poller = self.poller(make_issue())
        poller.poll_once()

        self.assertEqual(self.source.calls, 0)


class PollIdempotencyTest(PollerTestCase):
    def test_repeated_polling_does_not_duplicate_tasks(self):
        poller = self.poller(make_issue())

        first = poller.poll_once()
        second = poller.poll_once()
        third = poller.poll_once()

        self.assertEqual(first.stored, 1)
        self.assertEqual(second.stored, 0)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(third.unchanged, 1)
        self.assertEqual(len(self.store.current_tasks()), 1)

    def test_repeated_polling_across_new_poller_instances(self):
        self.poller(make_issue()).poll_once()
        report = self.poller(make_issue()).poll_once()

        self.assertEqual(report.unchanged, 1)
        self.assertEqual(len(self.store.current_tasks()), 1)

    def test_edited_issue_is_polled_as_a_new_revision(self):
        poller = self.poller(make_issue())
        poller.poll_once()

        self.source.replace(make_issue(body=body_replacing("Priority", "urgent")))
        report = poller.poll_once()

        self.assertEqual(len(report.revised), 1)
        self.assertEqual(len(self.store.revisions("ATLAS-0042")), 2)
        self.assertEqual(len(self.store.current_tasks()), 1)

    def test_issue_becoming_invalid_leaves_the_stored_revision_untouched(self):
        poller = self.poller(make_issue())
        poller.poll_once()

        self.source.replace(make_issue(body=body_without("Objective")))
        report = poller.poll_once()

        self.assertEqual(report.invalid, 1)
        self.assertEqual(report.stored, 0)
        self.assertEqual(len(self.store.current_tasks()), 1)


class PollCursorTest(PollerTestCase):
    def test_cursor_advances_to_the_newest_updated_at(self):
        poller = self.poller(
            make_issue(number=1, updated_at="2026-09-01T01:00:00Z"),
            make_issue(number=2, updated_at="2026-09-01T05:00:00Z"),
        )
        poller.poll_once()

        self.assertEqual(self.store.cursor("hongwon1031/atlas"), "2026-09-01T05:00:00Z")

    def test_saved_cursor_is_sent_on_the_next_pass(self):
        poller = self.poller(make_issue(updated_at="2026-09-01T05:00:00Z"))
        poller.poll_once()
        poller.poll_once()

        self.assertEqual(self.source.list_calls, [None, "2026-09-01T05:00:00Z"])

    def test_cursor_survives_reopen(self):
        self.poller(make_issue(updated_at="2026-09-01T05:00:00Z")).poll_once()
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


class PollLoopTest(PollerTestCase):
    def test_run_uses_the_configured_interval_between_passes(self):
        poller = self.poller(make_issue(), config=PollingConfig(interval_seconds=30.0))
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
        poller = self.poller(make_issue())

        reports = poller.run(max_iterations=2, sleep=lambda _: None)

        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0].stored, 1)
        self.assertEqual(reports[1].unchanged, 1)


class PollToClaimTest(PollerTestCase):
    def test_polled_task_becomes_claimable(self):
        self.poller(make_issue()).poll_once()

        claim = self.store.claim("worker-a", 900)

        self.assertIsNotNone(claim)
        self.assertEqual(claim.task_id, "ATLAS-0042")

    def test_invalid_issue_is_never_claimable(self):
        self.poller(make_issue(body=body_without("Objective"))).poll_once()

        self.assertIsNone(self.store.claim("worker-a", 900))


if __name__ == "__main__":
    unittest.main()
