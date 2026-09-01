import unittest

from atlas.intake import build_idempotency_key
from atlas.parser import parse_issue_body
from atlas.schema import Priority, RiskLevel, Severity, TaskStatus
from atlas.validation import REQUIRED_FIELDS, classify_scope, validate_intake
from tests.fixtures import VALID_BODY, body_replacing, body_without, make_issue


def run(issue):
    return validate_intake(issue, parse_issue_body(issue.body), build_idempotency_key(issue))


def codes(issues):
    return {issue.code for issue in issues}


class ValidIssueTest(unittest.TestCase):
    def setUp(self):
        self.result = run(make_issue())

    def test_returns_draft_task(self):
        self.assertTrue(self.result.is_valid)
        self.assertEqual(self.result.status, TaskStatus.DRAFT)
        self.assertEqual(self.result.errors, ())
        self.assertEqual(self.result.task.status, TaskStatus.DRAFT)

    def test_maps_form_fields_to_task_fields(self):
        task = self.result.task

        self.assertEqual(task.task_id, "ATLAS-0042")
        self.assertEqual(task.workspace_id, "personal")
        self.assertEqual(task.project_id, "atlas")
        self.assertEqual(task.repository, "hongwon1031/atlas")
        self.assertEqual(task.risk_level, RiskLevel.DOCUMENTATION)
        self.assertEqual(task.priority, Priority.NORMAL)
        self.assertEqual(len(task.constraints), 2)
        self.assertEqual(len(task.acceptance_criteria), 2)
        self.assertEqual(len(task.validation_plan), 2)

    def test_assigns_stable_criterion_ids(self):
        ids = [criterion.id for criterion in self.result.task.acceptance_criteria]
        self.assertEqual(ids, ["AC-01", "AC-02"])

    def test_records_source_and_delivery_policy(self):
        task = self.result.task

        self.assertEqual(task.source.channel, "github_issue")
        self.assertEqual(task.source.actor, "github:hongwon1031")
        self.assertEqual(task.delivery["base_branch"], "main")
        self.assertTrue(task.delivery["human_merge_approval_required"])

    def test_execution_is_manual_with_accepted_primary_adapter(self):
        execution = self.result.task.execution

        self.assertEqual(execution["dispatch_mode"], "manual")
        self.assertEqual(execution["primary_adapter"], "claude_code_self_hosted")
        self.assertIsNone(execution["claim_id"])
        self.assertIsNone(execution["active_run_id"])

    def test_planner_owned_fields_stay_empty(self):
        task = self.result.task

        self.assertEqual(task.context_refs, ())
        self.assertEqual(task.required_capabilities, ())
        self.assertIsNone(task.acceptance_criteria[0].verification)

    def test_advises_that_verification_is_unspecified(self):
        self.assertIn("verification_unspecified", codes(self.result.advisories))


class InvalidIssueTest(unittest.TestCase):
    def assert_rejected(self, issue, expected_code):
        result = run(issue)

        self.assertFalse(result.is_valid)
        self.assertIsNone(result.task)
        self.assertEqual(result.status, TaskStatus.NEEDS_CLARIFICATION)
        self.assertIn(expected_code, codes(result.errors))
        return result

    def test_missing_required_field(self):
        result = self.assert_rejected(
            make_issue(body=body_without("Objective")), "missing_required_field"
        )
        missing = {issue.field for issue in result.errors if issue.code == "missing_required_field"}
        self.assertEqual(missing, {"objective"})

    def test_reports_every_missing_field_at_once(self):
        result = run(make_issue(body=body_without("Objective", "Constraints", "Validation")))
        missing = {issue.field for issue in result.errors if issue.code == "missing_required_field"}

        self.assertEqual(missing, {"objective", "constraints", "validation"})

    def test_empty_body_reports_all_required_fields(self):
        result = run(make_issue(body=""))
        missing = {issue.field for issue in result.errors if issue.code == "missing_required_field"}

        self.assertFalse(result.is_valid)
        self.assertEqual(missing, {field for field, _ in REQUIRED_FIELDS})

    def test_invalid_risk_level(self):
        self.assert_rejected(
            make_issue(body=body_replacing("Risk Level", "catastrophic")), "invalid_risk_level"
        )

    def test_invalid_priority(self):
        self.assert_rejected(
            make_issue(body=body_replacing("Priority", "immediately")), "invalid_priority"
        )

    def test_project_outside_allowlist(self):
        self.assert_rejected(
            make_issue(body=body_replacing("Project", "other-project")), "project_not_allowed"
        )

    def test_repository_outside_allowlist(self):
        self.assert_rejected(make_issue(repository="someone/else"), "repository_not_allowed")

    def test_missing_task_form_marker(self):
        self.assert_rejected(make_issue(title="Please fix the docs"), "missing_task_form_marker")

    def test_closed_issue(self):
        self.assert_rejected(make_issue(state="closed"), "issue_not_open")

    def test_pull_request_is_not_a_task(self):
        self.assert_rejected(make_issue(is_pull_request=True), "issue_is_pull_request")

    def test_unchecked_safety_confirmation(self):
        body = VALID_BODY.replace(
            "- [X] AI가 `main`에", "- [ ] AI가 `main`에"
        )
        self.assert_rejected(make_issue(body=body), "safety_confirmation_unchecked")

    def test_duplicate_section(self):
        self.assert_rejected(make_issue(body=VALID_BODY + "\n### Objective\n\nsecond\n"), "duplicate_field")

    def test_scope_conflict(self):
        self.assert_rejected(
            make_issue(body=body_replacing("Forbidden Scope", "- docs/**")), "scope_conflict"
        )

    def test_errors_carry_human_readable_messages(self):
        result = run(make_issue(body=body_without("Objective")))

        for issue in result.errors:
            self.assertEqual(issue.severity, Severity.ERROR)
            self.assertTrue(issue.message.strip())


class ScopeClassificationTest(unittest.TestCase):
    def test_splits_paths_operations_and_external_systems(self):
        scope = classify_scope(("docs/**", "create", "production", "application code"))

        self.assertEqual(scope.paths, ("docs/**",))
        self.assertEqual(scope.operations, ("create",))
        self.assertEqual(scope.external_systems, ("production",))
        self.assertEqual(scope.unclassified, ("application code",))

    def test_unclassified_entries_are_preserved_not_dropped(self):
        scope = classify_scope(("무엇이든 자유 서술",))

        self.assertEqual(scope.unclassified, ("무엇이든 자유 서술",))
        self.assertEqual(scope.entries(), ("무엇이든 자유 서술",))


class AdvisoryTest(unittest.TestCase):
    def test_secrets_deployment_is_flagged_but_still_valid(self):
        result = run(make_issue(body=body_replacing("Risk Level", "secrets_deployment")))

        self.assertTrue(result.is_valid)
        self.assertIn("secrets_deployment_not_auto_dispatchable", codes(result.advisories))

    def test_missing_allowed_operations_is_advisory_only(self):
        body = body_replacing("Allowed Scope", "- docs/**")
        result = run(make_issue(body=body))

        self.assertTrue(result.is_valid)
        self.assertIn("allowed_operations_missing", codes(result.advisories))

    def test_unknown_section_is_advisory_only(self):
        result = run(make_issue(body=VALID_BODY + "\n### Extra Notes\n\nhello\n"))

        self.assertTrue(result.is_valid)
        self.assertIn("unknown_section", codes(result.advisories))


if __name__ == "__main__":
    unittest.main()
