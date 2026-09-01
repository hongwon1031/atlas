import unittest

from atlas.idempotency import InProcessIntakeCache, compute_issue_revision, derive_task_id
from atlas.intake import IssueIntake, build_idempotency_key
from atlas.issue_source import IssueSourceError
from tests.fixtures import VALID_BODY, FakeIssueSource, make_issue


class IntakeFlowTest(unittest.TestCase):
    def test_valid_issue_produces_task(self):
        intake = IssueIntake(FakeIssueSource(make_issue()))

        result = intake.intake(42)

        self.assertTrue(result.is_valid)
        self.assertEqual(result.task.task_id, "ATLAS-0042")

    def test_unknown_issue_raises_classified_error(self):
        intake = IssueIntake(FakeIssueSource(make_issue()))

        with self.assertRaises(IssueSourceError) as caught:
            intake.intake(999)

        self.assertEqual(caught.exception.category, "not_found")


class RepositoryBoundaryTest(unittest.TestCase):
    def test_repository_outside_allowlist_is_rejected_without_fetching(self):
        """경계 확인 전에 다른 repository를 읽으면 안 됩니다."""

        source = FakeIssueSource(make_issue())
        result = IssueIntake(source).intake(42, repository="someone/private-repo")

        self.assertEqual(source.calls, 0)
        self.assertFalse(result.is_valid)
        self.assertEqual({issue.code for issue in result.errors}, {"repository_not_allowed"})

    def test_pre_fetch_rejection_has_no_idempotency_fingerprint(self):
        result = IssueIntake(FakeIssueSource()).intake(42, repository="someone/private-repo")

        self.assertIsNone(result.idempotency_fingerprint)

    def test_allowed_repository_still_fetches(self):
        source = FakeIssueSource(make_issue())
        IssueIntake(source).intake(42)

        self.assertEqual(source.calls, 1)


class DeduplicationTest(unittest.TestCase):
    def test_second_intake_of_same_revision_is_deduplicated(self):
        intake = IssueIntake(FakeIssueSource(make_issue()))

        first = intake.intake(42)
        second = intake.intake(42)

        self.assertFalse(first.deduplicated)
        self.assertTrue(second.deduplicated)
        self.assertEqual(first.idempotency_fingerprint, second.idempotency_fingerprint)
        self.assertEqual(first.task, second.task)

    def test_deduplication_also_applies_to_invalid_issues(self):
        intake = IssueIntake(FakeIssueSource(make_issue(body="")))

        first = intake.intake(42)
        second = intake.intake(42)

        self.assertFalse(first.is_valid)
        self.assertTrue(second.deduplicated)
        self.assertEqual(first.errors, second.errors)

    def test_cache_is_not_shared_between_different_issues(self):
        source = FakeIssueSource(make_issue(number=1), make_issue(number=2))
        intake = IssueIntake(source)

        first = intake.intake(1)
        second = intake.intake(2)

        self.assertFalse(second.deduplicated)
        self.assertNotEqual(first.idempotency_fingerprint, second.idempotency_fingerprint)

    def test_closing_the_issue_invalidates_the_cached_result(self):
        """state는 내용 hash에 없으므로 cache가 stale Draft를 돌려주면 안 됩니다."""

        cache = InProcessIntakeCache()
        first = IssueIntake(FakeIssueSource(make_issue()), cache).intake(42)
        second = IssueIntake(FakeIssueSource(make_issue(state="closed")), cache).intake(42)

        self.assertTrue(first.is_valid)
        self.assertFalse(second.deduplicated)
        self.assertFalse(second.is_valid)
        self.assertIn("issue_not_open", {issue.code for issue in second.errors})

    def test_becoming_a_pull_request_invalidates_the_cached_result(self):
        cache = InProcessIntakeCache()
        IssueIntake(FakeIssueSource(make_issue()), cache).intake(42)
        second = IssueIntake(FakeIssueSource(make_issue(is_pull_request=True)), cache).intake(42)

        self.assertFalse(second.deduplicated)
        self.assertIn("issue_is_pull_request", {issue.code for issue in second.errors})

    def test_edited_issue_body_produces_a_new_key(self):
        cache = InProcessIntakeCache()
        original = make_issue()
        edited = make_issue(body=VALID_BODY.replace("normal", "high"))

        first = IssueIntake(FakeIssueSource(original), cache).intake(42)
        second = IssueIntake(FakeIssueSource(edited), cache).intake(42)

        self.assertFalse(second.deduplicated)
        self.assertNotEqual(first.idempotency_fingerprint, second.idempotency_fingerprint)


class IdempotencyKeyTest(unittest.TestCase):
    def test_key_carries_every_documented_component(self):
        key = build_idempotency_key(make_issue())

        self.assertEqual(key.repository_id, "123456")
        self.assertEqual(key.issue_id, "issue-42")
        self.assertEqual(key.signal_type, "manual_intake")
        self.assertEqual(key.task_id, "ATLAS-0042")
        self.assertTrue(key.issue_revision)

    def test_revision_ignores_line_ending_and_trailing_whitespace(self):
        self.assertEqual(
            compute_issue_revision("t", "a\nb"),
            compute_issue_revision("t", "a  \r\nb\r\n"),
        )

    def test_revision_changes_with_content(self):
        self.assertNotEqual(compute_issue_revision("t", "a"), compute_issue_revision("t", "b"))

    def test_task_id_is_zero_padded(self):
        self.assertEqual(derive_task_id(7), "ATLAS-0007")
        self.assertEqual(derive_task_id(1234), "ATLAS-1234")


if __name__ == "__main__":
    unittest.main()
