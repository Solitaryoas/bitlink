"""按脚本产出报告与 JSONL 的假执行器，不调用真实 codex。"""

import json
import os
import time

from bitlink.executors.base import ExecResult

DEFAULT_REPORT = {
    "status": "completed",
    "summary": "假执行器完成",
    "evidence": ["tests: 42 passed"],
    "files_changed": ["README.md"],
    "blockers": [],
}


class FakeExecutor:
    def __init__(
        self,
        report=None,
        sleep_seconds=0.0,
        exit_code=0,
        missing_report=False,
        invalid_report=False,
        session_id="exec-session-1",
        on_run=None,
    ):
        self.report = DEFAULT_REPORT if report is None else report
        self.sleep_seconds = sleep_seconds
        self.exit_code = exit_code
        self.missing_report = missing_report
        self.invalid_report = invalid_report
        self.session_id = session_id
        self.on_run = on_run
        self.calls = []

    def run(self, task, worktree_path, attempt_dir, timeout_seconds):
        os.makedirs(attempt_dir, exist_ok=True)
        self.calls.append(task["id"])
        if self.on_run is not None:
            self.on_run(task, worktree_path, attempt_dir)

        jsonl_path = os.path.join(attempt_dir, "events.jsonl")
        with open(jsonl_path, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"type": "thread.started", "thread_id": self.session_id}) + "\n"
            )
        stderr_path = os.path.join(attempt_dir, "stderr.log")
        with open(stderr_path, "w", encoding="utf-8") as handle:
            handle.write("")
        report_path = os.path.join(attempt_dir, "report.json")

        timed_out = timeout_seconds is not None and self.sleep_seconds > timeout_seconds
        if timed_out:
            return ExecResult(
                exit_code=None,
                jsonl_path=jsonl_path,
                report_path=report_path,
                stderr_path=stderr_path,
                session_id=self.session_id,
                timed_out=True,
            )

        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)

        if not self.missing_report:
            text = "{invalid json" if self.invalid_report else json.dumps(
                self.report, ensure_ascii=False
            )
            with open(report_path, "w", encoding="utf-8") as handle:
                handle.write(text)

        return ExecResult(
            exit_code=self.exit_code,
            jsonl_path=jsonl_path,
            report_path=report_path,
            stderr_path=stderr_path,
            session_id=self.session_id,
            timed_out=False,
        )
