"""报告解析：模型输出不一定是纯 JSON，load_report 要能提取并规范化。"""

import json
import os
import tempfile
import unittest

from bitlink.daemon import load_report


REPORT = {
    "status": "completed",
    "summary": "done",
    "evidence": ["pytest: 3 passed"],
    "files_changed": ["a.py"],
    "blockers": [],
}


class LoadReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "report.json")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_plain_json_is_accepted(self):
        self.write(json.dumps(REPORT))
        payload, error = load_report(self.path)
        self.assertIsNone(error)
        self.assertEqual(payload["summary"], "done")

    def test_prose_with_embedded_json_is_accepted(self):
        self.write("Task done.\n" + json.dumps(REPORT) + "\n希望有帮助。")
        payload, error = load_report(self.path)
        self.assertIsNone(error)
        self.assertEqual(payload["files_changed"], ["a.py"])

    def test_markdown_fenced_json_is_accepted(self):
        self.write("```json\n" + json.dumps(REPORT) + "\n```")
        payload, error = load_report(self.path)
        self.assertIsNone(error)
        self.assertEqual(payload["status"], "completed")

    def test_alias_fields_are_normalized_and_rewritten(self):
        self.write(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "done",
                    "verification": [{"command": "ls", "output": "a.py"}],
                    "changes": [{"path": "a.py", "action": "created"}],
                }
            )
        )
        payload, error = load_report(self.path)
        self.assertIsNone(error)
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(len(payload["evidence"]), 1)
        self.assertIn("a.py", payload["files_changed"][0])
        with open(self.path, "r", encoding="utf-8") as handle:
            rewritten = json.load(handle)
        self.assertTrue(
            {"status", "summary", "evidence", "files_changed", "blockers"} <= set(rewritten)
        )
        self.assertNotIn("changes", rewritten)
        self.assertNotIn("verification", rewritten)

    def test_invalid_status_enum_is_rejected(self):
        payload = dict(REPORT)
        payload["status"] = "done"
        self.write(json.dumps(payload))
        _, error = load_report(self.path)
        self.assertEqual(error, "report_invalid")

    def test_blank_evidence_items_count_as_empty(self):
        payload = dict(REPORT)
        payload["evidence"] = ["", "  "]
        self.write(json.dumps(payload))
        normalized, error = load_report(self.path)
        self.assertIsNone(error)
        self.assertEqual(normalized["evidence"], [])

    def test_garbage_is_rejected(self):
        self.write("这里是结论，没有 JSON。")
        payload, error = load_report(self.path)
        self.assertIsNone(payload)
        self.assertEqual(error, "report_invalid")

    def test_missing_file(self):
        payload, error = load_report(self.path)
        self.assertIsNone(payload)
        self.assertEqual(error, "report_missing")


if __name__ == "__main__":
    unittest.main()
