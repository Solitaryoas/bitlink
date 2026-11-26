import json
import os
import sys
import tempfile
import unittest

from bitlink.daemon import load_report
from bitlink.executors import codex
from bitlink.executors.base import ExecResult

FAKE_CODEX_SOURCE = r'''
import json
import os
import sys
import time

args = sys.argv[1:]
log_path = os.environ.get("FAKE_CODEX_ARGV_LOG")
if log_path:
    with open(log_path, "w", encoding="utf-8") as handle:
        json.dump(args, handle)

sleep_seconds = float(os.environ.get("FAKE_CODEX_SLEEP", "0") or 0)
if sleep_seconds:
    time.sleep(sleep_seconds)

report_path = None
for index, value in enumerate(args):
    if value == "-o":
        report_path = args[index + 1]

print(json.dumps({"type": "thread.started", "thread_id": "sess-exec-1"}), flush=True)

report_json = os.environ.get("FAKE_CODEX_REPORT")
if report_path and report_json is not None:
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(report_json)

worktree = None
for index, value in enumerate(args):
    if value == "-C":
        worktree = args[index + 1]
fallback = os.environ.get("FAKE_CODEX_FALLBACK_REPORT")
if worktree and fallback is not None:
    with open(os.path.join(worktree, ".bitlink-report.json"), "w", encoding="utf-8") as handle:
        handle.write(fallback)

sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0") or 0))
'''

TASK = {
    "id": "task1",
    "title": "改 README",
    "description": "补一行说明",
    "acceptance": "README 含说明",
}


class CodexExecutorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.script = os.path.join(self.dir, "fake_codex.py")
        with open(self.script, "w", encoding="utf-8") as handle:
            handle.write(FAKE_CODEX_SOURCE)
        self.attempt_dir = os.path.join(self.dir, "attempt")
        self.argv_log = os.path.join(self.dir, "argv.json")

    def tearDown(self):
        self._tmp.cleanup()

    def make_executor(self, **env):
        base = {"FAKE_CODEX_ARGV_LOG": self.argv_log}
        base.update(env)
        return codex.CodexExecutor(
            executable=[sys.executable, self.script],
            project_root=self.dir,
            env=base,
        )

    def test_command_contains_sandbox_and_schema_flags(self):
        executor = self.make_executor(FAKE_CODEX_REPORT=json.dumps(_report()))
        result = executor.run(TASK, self.dir, self.attempt_dir, 30)
        self.assertIsInstance(result, ExecResult)
        with open(self.argv_log, "r", encoding="utf-8") as handle:
            argv = json.load(handle)
        self.assertIn("-C", argv)
        self.assertEqual(argv[argv.index("-C") + 1], self.dir)
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertIn("--json", argv)
        self.assertIn("--output-schema", argv)
        schema_path = argv[argv.index("--output-schema") + 1]
        self.assertTrue(schema_path.endswith("report.schema.json"))
        self.assertTrue(os.path.isfile(schema_path))
        self.assertEqual(argv[argv.index("-o") + 1], os.path.join(self.attempt_dir, "report.json"))
        self.assertIn("README 含说明", argv[-1])
        self.assertIn("AGENTS.md", argv[-1])

    def test_stdout_is_written_to_events_jsonl_and_session_parsed(self):
        executor = self.make_executor(FAKE_CODEX_REPORT=json.dumps(_report()))
        result = executor.run(TASK, self.dir, self.attempt_dir, 30)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.session_id, "sess-exec-1")
        self.assertTrue(os.path.isfile(result.jsonl_path))
        with open(result.jsonl_path, "r", encoding="utf-8") as handle:
            first = json.loads(handle.readline())
        self.assertEqual(first["thread_id"], "sess-exec-1")
        self.assertTrue(os.path.isfile(result.report_path))
        self.assertTrue(os.path.isfile(result.stderr_path))

    def test_timeout_marks_timed_out(self):
        executor = self.make_executor(FAKE_CODEX_SLEEP="3")
        result = executor.run(TASK, self.dir, self.attempt_dir, 1)
        self.assertTrue(result.timed_out)

    def test_non_zero_exit_code_is_propagated(self):
        executor = self.make_executor(FAKE_CODEX_EXIT="7")
        result = executor.run(TASK, self.dir, self.attempt_dir, 30)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.exit_code, 7)

    def test_prompt_inlines_agents_and_forbids_git(self):
        with open(os.path.join(self.dir, "AGENTS.md"), "w", encoding="utf-8") as handle:
            handle.write("项目约定：提交信息用中文")
        prompt = codex.build_prompt(TASK, self.dir, self.dir, os.path.join(self.attempt_dir, "report.json"))
        self.assertIn("项目约定：提交信息用中文", prompt)
        self.assertIn("不要执行任何 git 命令", prompt)
        self.assertIn(".bitlink-report.json", prompt)

    def test_falls_back_to_workspace_report_when_final_message_is_prose(self):
        body = json.dumps(_report())
        executor = self.make_executor(
            FAKE_CODEX_REPORT="Task done. 下面是报告。",
            FAKE_CODEX_FALLBACK_REPORT=body,
        )
        result = executor.run(TASK, self.dir, self.attempt_dir, 30)
        with open(result.report_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["summary"], "done")
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".bitlink-report.json")))

    @unittest.skipUnless(os.name == "nt", "Windows 专有的沙箱参数")
    def test_windows_execution_bypasses_sandbox_by_default(self):
        executor = self.make_executor(FAKE_CODEX_REPORT=json.dumps(_report()))
        executor.run(TASK, self.dir, self.attempt_dir, 30)
        with open(self.argv_log, "r", encoding="utf-8") as handle:
            argv = json.load(handle)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_non_report_json_does_not_discard_valid_fallback(self):
        body = json.dumps(_report())
        executor = self.make_executor(
            FAKE_CODEX_REPORT='{"ok": true}',
            FAKE_CODEX_FALLBACK_REPORT=body,
        )
        result = executor.run(TASK, self.dir, self.attempt_dir, 30)
        with open(result.report_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["summary"], "done")
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".bitlink-report.json")))

    def test_stale_fallback_from_previous_attempt_is_removed(self):
        stale = os.path.join(self.dir, ".bitlink-report.json")
        with open(stale, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(_report()))
        executor = self.make_executor(FAKE_CODEX_REPORT="没有报告的散文。")
        result = executor.run(TASK, self.dir, self.attempt_dir, 30)
        self.assertFalse(os.path.exists(stale))
        _, error = load_report(result.report_path)
        self.assertIsNotNone(error)

    @unittest.skipIf(os.name == "nt", "只在非 Windows 上验证不加 bypass 参数")
    def test_posix_does_not_bypass_sandbox(self):
        executor = self.make_executor(FAKE_CODEX_REPORT=json.dumps(_report()))
        executor.run(TASK, self.dir, self.attempt_dir, 30)
        with open(self.argv_log, "r", encoding="utf-8") as handle:
            argv = json.load(handle)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_report_schema_is_parseable_and_requires_fields(self):
        schema = _load_schema("report.schema.json")
        self.assertEqual(schema["type"], "object")
        self.assertEqual(
            set(schema["required"]),
            {"status", "summary", "evidence", "files_changed", "blockers"},
        )
        self.assertEqual(schema["properties"]["status"]["enum"], ["completed", "failed", "blocked"])
        self.assertEqual(schema["properties"]["evidence"]["type"], "array")
        self.assertFalse(schema["additionalProperties"])

    def test_review_schema_is_parseable_and_requires_fields(self):
        schema = _load_schema("review.schema.json")
        self.assertEqual(set(schema["required"]), {"verdict", "reason", "findings"})
        self.assertEqual(schema["properties"]["verdict"]["enum"], ["pass", "reject"])
        self.assertFalse(schema["additionalProperties"])


def _report():
    return {
        "status": "completed",
        "summary": "done",
        "evidence": ["git status 输出为空"],
        "files_changed": ["README.md"],
        "blockers": [],
    }


def _load_schema(name):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "bitlink",
        "schemas",
        name,
    )
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


if __name__ == "__main__":
    unittest.main()
