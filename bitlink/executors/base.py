"""执行器 seam：只暴露 run(...) 与结构化结果。"""

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class ExecResult:
    exit_code: Optional[int]
    jsonl_path: str
    report_path: str
    stderr_path: str
    session_id: Optional[str]
    timed_out: bool


class Executor(Protocol):
    def run(self, task, worktree_path, attempt_dir, timeout_seconds) -> ExecResult:
        """在 worktree 中执行任务卡，返回可落库的结果。"""
