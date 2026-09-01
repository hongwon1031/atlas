"""CLI가 내보내는 JSON 형태를 고정합니다."""

import json
import unittest

from atlas.intake import IssueIntake
from tests.fixtures import FakeIssueSource, make_issue


def dump(result):
    return json.loads(json.dumps(result.to_dict(), ensure_ascii=False))


class ValidResultShapeTest(unittest.TestCase):
    def setUp(self):
        self.payload = dump(IssueIntake(FakeIssueSource(make_issue())).intake(42))

    def test_top_level_keys(self):
        self.assertEqual(
            set(self.payload),
            {"status", "is_valid", "deduplicated", "idempotency_fingerprint", "task", "errors", "advisories"},
        )

    def test_enums_serialize_as_documented_values(self):
        task = self.payload["task"]

        self.assertEqual(self.payload["status"], "Draft")
        self.assertEqual(task["status"], "Draft")
        self.assertEqual(task["risk_level"], "documentation")
        self.assertEqual(task["priority"], "normal")

    def test_task_exposes_schema_version_and_scope_structure(self):
        task = self.payload["task"]

        self.assertEqual(task["schema_version"], "0.1")
        self.assertEqual(task["allowed_scope"]["paths"], ["docs/**", "src/**"])
        self.assertEqual(task["allowed_scope"]["operations"], ["create", "update"])

    def test_audit_carries_idempotency_key(self):
        key = self.payload["task"]["audit"]["idempotency"]

        self.assertEqual(key["signal_type"], "manual_intake")
        self.assertEqual(key["task_id"], "ATLAS-0042")


class InvalidResultShapeTest(unittest.TestCase):
    def test_invalid_result_has_null_task_and_error_list(self):
        payload = dump(IssueIntake(FakeIssueSource(make_issue(body=""))).intake(42))

        self.assertFalse(payload["is_valid"])
        self.assertEqual(payload["status"], "NeedsClarification")
        self.assertIsNone(payload["task"])
        self.assertTrue(payload["errors"])
        for error in payload["errors"]:
            self.assertEqual(set(error), {"code", "message", "severity", "field"})
            self.assertEqual(error["severity"], "error")


if __name__ == "__main__":
    unittest.main()
