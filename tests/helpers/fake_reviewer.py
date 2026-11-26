"""按预设 verdict 返回结果的假 reviewer。"""

import json
import os

from bitlink.review import ReviewResult


class FakeReviewer:
    def __init__(self, verdicts=("pass",), reason="fake review ok", session_id="review-session-1"):
        self.verdicts = list(verdicts) or ["pass"]
        self.reason = reason
        self.session_id = session_id
        self.calls = []

    def review(self, task, worktree_path, attempt_dir, diff_path, report_path, timeout_seconds):
        os.makedirs(attempt_dir, exist_ok=True)
        self.calls.append(task["id"])
        verdict = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]

        jsonl_path = os.path.join(attempt_dir, "review.jsonl")
        with open(jsonl_path, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"type": "thread.started", "thread_id": self.session_id}) + "\n"
            )
        result_path = os.path.join(attempt_dir, "review.json")
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(
                {"verdict": verdict, "reason": self.reason, "findings": []},
                handle,
                ensure_ascii=False,
            )

        return ReviewResult(
            verdict=verdict,
            reason=self.reason,
            findings=[],
            session_id=self.session_id,
            jsonl_path=jsonl_path,
            report_path=result_path,
        )
