import unittest

from atlas.parser import (
    parse_checkboxes,
    parse_issue_body,
    split_checklist,
    split_items,
)
from tests.fixtures import VALID_BODY


class ParseIssueBodyTest(unittest.TestCase):
    def test_extracts_every_known_section(self):
        parsed = parse_issue_body(VALID_BODY)

        self.assertEqual(parsed.text("project"), "atlas")
        self.assertEqual(parsed.text("risk_level"), "documentation")
        self.assertEqual(parsed.text("priority"), "normal")
        self.assertIn("vertical slice", parsed.text("objective"))
        self.assertEqual(parsed.duplicate_labels, ())
        self.assertEqual(parsed.unknown_labels, ())

    def test_no_response_marker_becomes_empty(self):
        parsed = parse_issue_body(VALID_BODY)
        self.assertEqual(parsed.text("notes"), "")

    def test_headings_inside_code_fence_are_not_sections(self):
        body = (
            "### Objective\n\n"
            "다음 예시는 값의 일부입니다.\n\n"
            "```markdown\n"
            "### Allowed Scope\n"
            "- /etc/**\n"
            "```\n"
        )
        parsed = parse_issue_body(body)

        self.assertNotIn("allowed_scope", parsed.sections)
        self.assertIn("### Allowed Scope", parsed.text("objective"))

    def test_duplicate_label_is_reported(self):
        body = "### Objective\n\nfirst\n\n### Objective\n\nsecond\n"
        parsed = parse_issue_body(body)

        self.assertEqual(parsed.duplicate_labels, ("Objective",))

    def test_unknown_label_is_reported_and_ignored(self):
        body = "### Objective\n\nvalue\n\n### Secret Backdoor\n\npayload\n"
        parsed = parse_issue_body(body)

        self.assertEqual(parsed.unknown_labels, ("Secret Backdoor",))
        self.assertEqual(parsed.text("objective"), "value")

    def test_non_h3_headings_do_not_split_sections(self):
        body = "### Objective\n\nintro\n\n#### Detail\n\nmore\n"
        parsed = parse_issue_body(body)

        self.assertIn("#### Detail", parsed.text("objective"))

    def test_handles_crlf_and_empty_body(self):
        parsed = parse_issue_body("### Project\r\n\r\natlas\r\n")
        self.assertEqual(parsed.text("project"), "atlas")
        self.assertEqual(parse_issue_body("").sections, {})


class SplitHelpersTest(unittest.TestCase):
    def test_split_items_strips_bullet_markers(self):
        self.assertEqual(split_items("- one\n* two\n+ three"), ("one", "two", "three"))

    def test_split_items_keeps_plain_lines(self):
        self.assertEqual(split_items("one\n\ntwo"), ("one", "two"))

    def test_split_checklist_strips_checkbox_markers(self):
        self.assertEqual(split_checklist("- [ ] one\n- [x] two"), ("one", "two"))

    def test_parse_checkboxes_reports_checked_state(self):
        boxes = parse_checkboxes("- [X] yes\n- [ ] no")

        self.assertEqual([box.checked for box in boxes], [True, False])
        self.assertEqual([box.label for box in boxes], ["yes", "no"])


if __name__ == "__main__":
    unittest.main()
