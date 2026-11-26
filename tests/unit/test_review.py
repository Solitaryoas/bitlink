import json
import os
import sys
import tempfile
import unittest

from bitlink import review
from bitlink.review import ReviewResult

FAKE_REVIEW_SOURCE = r'''
import json
import os
import sys

args = sys.argv[1:]
log_path = os.environ.get("FAKE_REVIEW_ARGV_LOG")
if log_path:
    with open(log_path, "w", encoding="utf-8") as handle:
        json.dump(args, handle)

output_path = None
for index, value in enumerate(args):
    if value == "-o":
        output_path = args[index + 1]

print(
    json.dumps(
        {"type": "thread.started", "thread_id": os.environ.get("FAKE_REVIEW_SESSION", "review-session-1")}
    ),
    flush=True,
)

content = os.environ.get("FAKE_REVIEW_JSON")
if output_path and content:
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(content)
'''

TASK = {
    "id": "task1",
    "title": "改 README",
    "description": "补一行说明",
    "acceptance": "README 含说明",
}


class reviewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.script = os.path.join(self.dir, "fake_review.py")
        with open(self.script, "w", encoding="utf-8") as handle:
            handle.write(FAKE_REVIEW_SOURCE)
        self.attempt_dir = os.path.join(self.dir, "attempt")
        self.argv_log = os.path.join(self.dir, "argv.json")

    def tearDown(self):
        self._tmp.cleanup()

    def make_reviewer(self, **env):
        base = {"FAKE_REVIEW_ARGV_LOG": self.argv_log}
        base.update(env)
        return review.CodexReviewer(
            executable=[sys.executable, self.script],
            project_root=self.dir,
            env=base,
        )

    def run_review(self, reviewer):
        return reviewer.review(
            TASK, self.dir, self.attempt_dir, os.path.join(self.dir, "diff.patch"), "report.json", 30
        )

    def test_ac5_review_uses_read_only_sandbox(self):
        reviewer = self.make_reviewer(
            FAKE_REVIEW_JSON=json.dumps({"verdict": "pass", "reason": "ok", "findings": []})
        )
        result = self.run_review(reviewer)
        self.assertEqual(result.verdict, "pass")
        with open(self.argv_log, "r", encoding="utf-8") as handle:
            argv = json.load(handle)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertNotIn("workspace-write", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--output-schema", argv)
        self.assertIn("验收标准", argv[-1])
        self.assertIn("README 含说明", argv[-1])

    def test_prose_with_embedded_json_is_accepted(self):
        body = {"verdict": "pass", "reason": "ok", "findings": []}
        reviewer = self.make_reviewer(FAKE_REVIEW_JSON="review 结论如下：\n" + json.dumps(body))
        result = self.run_review(reviewer)
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.reason, "ok")

    def test_prompt_inlines_diff_content(self):
        diff_path = os.path.join(self.dir, "diff.patch")
        with open(diff_path, "w", encoding="utf-8") as handle:
            handle.write("diff --git a/x.py b/x.py\n+print('hello')\n")
        reviewer = self.make_reviewer(
            FAKE_REVIEW_JSON=json.dumps({"verdict": "pass", "reason": "ok", "findings": []})
        )
        self.run_review(reviewer)
        with open(self.argv_log, "r", encoding="utf-8") as handle:
            argv = json.load(handle)
        self.assertIn("print('hello')", argv[-1])
        self.assertIn("不要执行 git 命令", argv[-1])

    def test_ac5_review_session_differs_from_execution_session(self):
        reviewer = self.make_reviewer(
            FAKE_REVIEW_SESSION="review-session-42",
            FAKE_REVIEW_JSON=json.dumps({"verdict": "pass", "reason": "ok", "findings": []}),
        )
        result = self.run_review(reviewer)
        self.assertEqual(result.session_id, "review-session-42")
        self.assertNotEqual(result.session_id, "exec-session-1")
        self.assertTrue(os.path.isfile(result.jsonl_path))
        self.assertTrue(os.path.isfile(result.report_path))

    def test_reject_verdict_is_propagated(self):
        reviewer = self.make_reviewer(
            FAKE_REVIEW_JSON=json.dumps(
                {
                    "verdict": "reject",
                    "reason": "缺少测试证据",
                    "findings": [{"severity": "high", "message": "没有测试输出"}],
                }
            )
        )
        result = self.run_review(reviewer)
        self.assertEqual(result.verdict, "reject")
        self.assertEqual(result.reason, "缺少测试证据")
        self.assertEqual(len(result.findings), 1)

    def test_invalid_json_becomes_reject_with_readable_reason(self):
        reviewer = self.make_reviewer(FAKE_REVIEW_JSON="{not json")
        result = self.run_review(reviewer)
        self.assertIsInstance(result, ReviewResult)
        self.assertEqual(result.verdict, "reject")
        self.assertIn("review_output_invalid", result.reason)

    def test_missing_output_becomes_reject(self):
        reviewer = self.make_reviewer()
        result = self.run_review(reviewer)
        self.assertEqual(result.verdict, "reject")
        self.assertEqual(result.reason, "review_output_missing")

    def test_unknown_verdict_becomes_reject(self):
        reviewer = self.make_reviewer(
            FAKE_REVIEW_JSON=json.dumps({"verdict": "maybe", "reason": "", "findings": []})
        )
        result = self.run_review(reviewer)
        self.assertEqual(result.verdict, "reject")
        self.assertIn("review_output_invalid", result.reason)

    def test_skips_echoed_task_card_and_takes_verdict_object(self):
        echoed = json.dumps({"id": "task1", "title": "改 README"})
        body = json.dumps({"verdict": "pass", "reason": "ok", "findings": []})
        reviewer = self.make_reviewer(FAKE_REVIEW_JSON=f"{echoed}\n{body}")
        result = self.run_review(reviewer)
        self.assertEqual(result.verdict, "pass")


if __name__ == "__main__":
    unittest.main()
