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

    def test_shorter_fence_inside_long_fence_does_not_close_it(self):
        body = (
            "### Objective\n\n"
            "````markdown\n"
            "```python\n"
            "### Allowed Scope\n"
            "example\n"
            "```\n"
            "````\n\n"
            "### Priority\n\nnormal\n"
        )
        parsed = parse_issue_body(body)

        self.assertNotIn("allowed_scope", parsed.sections)
        self.assertIn("### Allowed Scope", parsed.text("objective"))
        self.assertEqual(parsed.text("priority"), "normal")

    def test_fence_with_trailing_text_does_not_close_it(self):
        body = (
            "### Objective\n\n"
            "```markdown\n"
            "```not-a-closing-fence\n"
            "### Priority\n"
            "urgent\n"
            "```\n\n"
            "### Priority\n\nnormal\n"
        )
        parsed = parse_issue_body(body)

        self.assertEqual(parsed.duplicate_labels, ())
        self.assertIn("### Priority", parsed.text("objective"))
        self.assertEqual(parsed.text("priority"), "normal")

    def test_duplicate_label_is_reported(self):
        body = "### Objective\n\nfirst\n\n### Objective\n\nsecond\n"
        parsed = parse_issue_body(body)

        self.assertEqual(parsed.duplicate_labels, ("Objective",))

    def test_unknown_label_is_reported_but_kept_as_content(self):
        body = "### Objective\n\nvalue\n\n### Secret Backdoor\n\npayload\n"
        parsed = parse_issue_body(body)

        self.assertEqual(parsed.unknown_labels, ("Secret Backdoor",))
        self.assertIn("value", parsed.text("objective"))
        self.assertIn("payload", parsed.text("objective"))

    def test_unknown_heading_does_not_truncate_the_field(self):
        """알 수 없는 `###` 뒤의 사용자 내용이 조용히 사라지면 안 됩니다."""

        body = (
            "### Objective\n\n서두를 쓴다.\n\n"
            "### Detail\n\n핵심 목표는 여기에 있다.\n\n"
            "### Priority\n\nnormal\n"
        )
        parsed = parse_issue_body(body)

        self.assertIn("서두를 쓴다.", parsed.text("objective"))
        self.assertIn("핵심 목표는 여기에 있다.", parsed.text("objective"))
        self.assertEqual(parsed.text("priority"), "normal")

    def test_unknown_heading_before_any_known_label_is_dropped(self):
        parsed = parse_issue_body("### Preamble\n\nnoise\n\n### Project\n\natlas\n")

        self.assertEqual(parsed.unknown_labels, ("Preamble",))
        self.assertEqual(parsed.text("project"), "atlas")

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
