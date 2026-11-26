"""review：独立、只读、不同于执行会话的 codex 进程，按验收标准判定。"""

import json
import os
from dataclasses import dataclass, field

from bitlink.jsonout import find_json_object
from bitlink.procs import first_session_id, run_process

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas")
REVIEW_SCHEMA_PATH = os.path.join(SCHEMA_DIR, "review.schema.json")

VALID_VERDICTS = ("pass", "reject")
DIFF_INLINE_LIMIT = 60000
REPORT_INLINE_LIMIT = 8000


@dataclass
class ReviewResult:
    verdict: str
    reason: str
    findings: list = field(default_factory=list)
    session_id: object = None
    jsonl_path: str = ""
    report_path: str = ""
    exit_code: object = None
    timed_out: bool = False


def _read_text(path, limit, fallback):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return fallback
    if len(text) > limit:
        return text[:limit] + "\n... (内容过长已截断)"
    return text


def build_review_prompt(task, worktree_path, diff_path, report_path):
    """把补丁和报告内联进提示：沙箱读不到工作区外的路径。"""
    diff_text = _read_text(diff_path, DIFF_INLINE_LIMIT, "(diff 不可读)")
    report_text = _read_text(report_path, REPORT_INLINE_LIMIT, "(报告不可读)")
    lines = [
        "你是独立 review 进程。请在只读沙箱里对照验收标准判断这次改动是否通过。",
        f"工作区：{worktree_path}",
        "沙箱约束：工作区之外的路径和 .git 不可读，不要执行 git 命令；下面的补丁与报告已内联，不需要再去读文件。",
        f"任务标题：{task.get('title')}",
        f"任务描述：{task.get('description')}",
        "验收标准原文：",
        str(task.get("acceptance") or ""),
        "改动补丁（diff.patch）：",
        diff_text,
        "执行报告（report.json）：",
        report_text,
        "逐条核对验收标准，必须基于补丁和报告里的磁盘事实判断；找不到证据就 reject。",
        "如果补丁显示「不可读」或含「内容过长已截断」，说明你看不到完整改动，必须 reject 并在 reason 里说明原因。",
        '最后一条消息只输出一个 JSON 对象：{"verdict":"pass|reject","reason":"...","findings":[{"severity":"P1","message":"..."}]}。'
        "不要 markdown 围栏，不要额外解释。",
    ]
    return "\n".join(lines)


def _parse_review(review_path, outcome):
    if outcome.timed_out:
        return "reject", "review_timeout", []
    if not os.path.isfile(review_path):
        return "reject", "review_output_missing", []
    try:
        with open(review_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        return "reject", f"review_output_invalid: {exc}", []

    def is_review(item):
        return str(item.get("verdict") or "").strip().lower() in VALID_VERDICTS

    payload = find_json_object(text, is_review)
    if payload is None:
        return "reject", "review_output_invalid: 找不到含 verdict 的 JSON 对象", []
    verdict = str(payload.get("verdict")).strip().lower()
    findings = payload.get("findings")
    if not isinstance(findings, list):
        findings = []
    reason = payload.get("reason")
    return verdict, str(reason or ""), findings


class CodexReviewer:
    """review 适配器；与执行器共用 procs.run_process，但沙箱只读。"""

    name = "codex-review"

    def __init__(
        self,
        executable="codex",
        project_root=None,
        schema_path=REVIEW_SCHEMA_PATH,
        model=None,
        env=None,
        registry=None,
        data_dir=None,
        sandbox_mode="service",
    ):
        self.executable = [executable] if isinstance(executable, str) else list(executable)
        self.project_root = project_root
        self.schema_path = schema_path
        self.model = model
        self.env = dict(os.environ) if env is None else {**os.environ, **env}
        self.registry = registry
        self.data_dir = data_dir
        self.sandbox_mode = sandbox_mode or "service"

    def build_command(self, worktree_path, review_json_path, prompt):
        command = [
            *self.executable,
            "exec",
            "-C",
            worktree_path,
            "-s",
            "read-only",
            "--json",
            "-o",
            review_json_path,
            "--output-schema",
            self.schema_path,
        ]
        if os.name == "nt" and self.sandbox_mode == "bypass":
            command.append("--dangerously-bypass-approvals-and-sandbox")
        if self.model:
            command.extend(["-m", self.model])
        command.append(prompt)
        return command

    def review(self, task, worktree_path, attempt_dir, diff_path, report_path, timeout_seconds):
        attempt_dir = os.path.abspath(attempt_dir)
        os.makedirs(attempt_dir, exist_ok=True)
        review_json_path = os.path.join(attempt_dir, "review.json")
        # 执行 agent 在 bypass 模式下有能力预写这个文件；开跑前清掉，避免伪造 pass
        if os.path.isfile(review_json_path):
            try:
                os.remove(review_json_path)
            except OSError:
                pass
        review_jsonl_path = os.path.join(attempt_dir, "review.jsonl")
        stderr_path = os.path.join(attempt_dir, "review_stderr.log")

        prompt = build_review_prompt(task, worktree_path, diff_path, report_path)
        command = self.build_command(worktree_path, review_json_path, prompt)
        outcome = run_process(
            command,
            review_jsonl_path,
            stderr_path,
            timeout_seconds,
            env=self.env,
            cwd=worktree_path,
            registry=self.registry,
            registry_key=task.get("id"),
            marker_dir=self.data_dir,
            marker_task_id=task.get("id") if self.data_dir else None,
        )
        verdict, reason, findings = _parse_review(review_json_path, outcome)
        return ReviewResult(
            verdict=verdict,
            reason=reason,
            findings=findings,
            session_id=first_session_id(review_jsonl_path),
            jsonl_path=review_jsonl_path,
            report_path=review_json_path,
            exit_code=outcome.exit_code,
            timed_out=outcome.timed_out,
        )
