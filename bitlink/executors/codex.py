"""codex 执行器：一条 codex exec 命令，报告以 JSON 形式回传。"""

import json
import os

from bitlink.executors.base import ExecResult
from bitlink.jsonout import find_json_object
from bitlink.procs import first_session_id, run_process
from bitlink.report import normalize_report

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schemas")
REPORT_SCHEMA_PATH = os.path.join(SCHEMA_DIR, "report.schema.json")
# 工作区内的报告副本；daemon 读走后删除，避免污染 diff
FALLBACK_REPORT_NAME = ".bitlink-report.json"
AGENTS_INLINE_LIMIT = 8000


def _read_agents(project_root):
    if not project_root:
        return None
    path = os.path.join(project_root, "AGENTS.md")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()[:AGENTS_INLINE_LIMIT]
    except OSError:
        return None


def build_prompt(task, worktree_path, project_root, report_path):
    card = {
        "id": task.get("id"),
        "title": task.get("title"),
        "description": task.get("description"),
        "acceptance": task.get("acceptance"),
    }
    agents = _read_agents(project_root)
    lines = [
        "你在一个隔离工作区中执行一张 BitLink 任务卡。",
        f"工作区绝对路径：{worktree_path}",
        "沙箱约束（必须遵守）：不要执行任何 git 命令，.git 可能不可读；"
        "版本控制、diff 和合并由 BitLink 负责，你只需要改工作区里的文件。",
    ]
    if agents:
        lines += ["项目说明（AGENTS.md 内容，直接作为约定使用）：", agents]
    elif project_root:
        lines.append("项目说明：项目根目录没有 AGENTS.md，以工作区现状为准。")

    lines += [
        "任务卡（JSON）：",
        json.dumps(card, ensure_ascii=False, indent=2),
        "完成后必须做两件事：",
        "1. 生成一个 JSON 对象，字段固定为 status（completed/failed/blocked）、summary、"
        "evidence（字符串数组）、files_changed（字符串数组）、blockers（字符串数组）。",
        "2. 把同一个 JSON 写到工作区根目录的 "
        f"{FALLBACK_REPORT_NAME}（BitLink 读取后会删除它）。",
        "evidence 必须是可复核的磁盘事实（执行的命令与输出、改动的文件路径）；"
        "没有证据就留空数组，不要编造，也不要把整个文件内容抄进来。",
        "最后一条消息只输出这个 JSON 对象本身，不要 markdown 围栏，不要额外解释。",
    ]
    return "\n".join(lines)


def _clear_fallback(worktree_path):
    """清掉上一次尝试残留的兜底报告，避免被当成本次结果采纳。"""
    fallback = os.path.join(worktree_path, FALLBACK_REPORT_NAME)
    if os.path.isfile(fallback):
        try:
            os.remove(fallback)
        except OSError:
            pass


def _read_report(path):
    """读文件并返回其中第一份符合契约的报告；没有则返回 None。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    payload = find_json_object(text, lambda item: normalize_report(item) is not None)
    return normalize_report(payload) if payload is not None else None


def _adopt_fallback_report(worktree_path, report_path):
    """`-o` 不是合规报告时，改用工作区内的 .bitlink-report.json 并清理它。

    只判「是不是 JSON」不够：回显任务卡、`{"ok": true}` 这类合法 JSON 会
    把正确的兜底报告误删，所以这里按报告契约判定。
    """
    fallback = os.path.join(worktree_path, FALLBACK_REPORT_NAME)
    if _read_report(report_path) is not None:
        if os.path.isfile(fallback):
            os.remove(fallback)
        return
    if os.path.isfile(fallback):
        payload = _read_report(fallback)
        if payload is not None:
            try:
                with open(report_path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                os.remove(fallback)
                return
            except OSError:
                pass
        try:
            os.remove(fallback)
        except OSError:
            pass


class CodexExecutor:
    name = "codex"

    def __init__(
        self,
        executable="codex",
        project_root=None,
        schema_path=REPORT_SCHEMA_PATH,
        model=None,
        env=None,
        registry=None,
        data_dir=None,
        sandbox_mode="bypass",
    ):
        self.executable = [executable] if isinstance(executable, str) else list(executable)
        self.project_root = project_root
        self.schema_path = schema_path
        self.model = model
        self.env = dict(os.environ) if env is None else {**os.environ, **env}
        self.registry = registry
        self.data_dir = data_dir
        self.sandbox_mode = sandbox_mode or "bypass"

    def build_command(self, worktree_path, report_path, prompt):
        command = [
            *self.executable,
            "exec",
            "-C",
            worktree_path,
            "-s",
            "workspace-write",
            "--json",
            "-o",
            report_path,
            "--output-schema",
            self.schema_path,
        ]
        # Windows 沙箱服务会给 agent 写出的文件加另一个用户的 ACL，daemon 读不到，
        # diff 和人工合并都没法做；bypass 模式下隔离边界只剩 worktree。
        if os.name == "nt" and self.sandbox_mode == "bypass":
            command.append("--dangerously-bypass-approvals-and-sandbox")
        if self.model:
            command.extend(["-m", self.model])
        command.append(prompt)
        return command

    def run(self, task, worktree_path, attempt_dir, timeout_seconds):
        attempt_dir = os.path.abspath(attempt_dir)
        os.makedirs(attempt_dir, exist_ok=True)
        report_path = os.path.join(attempt_dir, "report.json")
        jsonl_path = os.path.join(attempt_dir, "events.jsonl")
        stderr_path = os.path.join(attempt_dir, "stderr.log")

        prompt = build_prompt(task, worktree_path, self.project_root, report_path)
        _clear_fallback(worktree_path)
        command = self.build_command(worktree_path, report_path, prompt)
        outcome = run_process(
            command,
            jsonl_path,
            stderr_path,
            timeout_seconds,
            env=self.env,
            cwd=worktree_path,
            registry=self.registry,
            registry_key=task.get("id"),
            marker_dir=self.data_dir,
            marker_task_id=task.get("id") if self.data_dir else None,
        )
        _adopt_fallback_report(worktree_path, report_path)
        return ExecResult(
            exit_code=outcome.exit_code,
            jsonl_path=jsonl_path,
            report_path=report_path,
            stderr_path=stderr_path,
            session_id=first_session_id(jsonl_path),
            timed_out=outcome.timed_out,
        )
