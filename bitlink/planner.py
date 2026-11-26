"""规划层：把一段自然语言需求拆成带验收标准的任务卡。

规划器是一个独立的只读 codex 进程，不碰数据库、不改仓库文件。它只负责产出
卡片草稿，是否落库由人决定（CLI 的 `--apply`）。这样 AI 参与拆解，但不会在
没人看过的情况下把自己的猜测写进队列。
"""

import json
import os
import subprocess
import uuid

from bitlink import cards as cards_module
from bitlink.jsonout import find_json_object
from bitlink.procs import first_session_id, run_process

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas")
PLAN_SCHEMA_PATH = os.path.join(SCHEMA_DIR, "plan.schema.json")

MAX_CARDS = 20
MAX_FACTS = 300
AGENTS_INLINE_LIMIT = 8000
PLANNER_FIELDS = ("title", "description", "acceptance", "max_attempts", "timeout_seconds")


class PlanError(ValueError):
    """规划输出不可用（模型没给 JSON、卡片不合法等）。"""


def collect_facts(project_root, limit=MAX_FACTS):
    """采集仓库事实：受版本控制的文件清单，超限截断并注明。"""
    try:
        completed = subprocess.run(
            ["git", "-C", str(project_root), "ls-files"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return "（无法列出文件：git 不可用）"
    files = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    if completed.returncode != 0 or not files:
        return "（仓库暂无可列出的受控文件）"
    shown = files[:limit]
    lines = [f"共 {len(files)} 个受控文件，按路径排序：" if len(files) > limit else f"共 {len(files)} 个受控文件："]
    lines.extend(f"- {path}" for path in shown)
    if len(files) > limit:
        lines.append(f"- ……其余 {len(files) - limit} 个文件省略")
    return "\n".join(lines)


def _read_agents(project_root):
    path = os.path.join(project_root, "AGENTS.md")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()[:AGENTS_INLINE_LIMIT]
    except OSError:
        return None


def build_plan_prompt(request, facts, existing_titles=(), agents_text=None, completed_titles=()):
    lines = [
        "你是 BitLink 的规划器。把下面这段需求拆成若干张可独立执行的任务卡。",
        "你只做规划：不要修改任何文件，不要执行 git 命令，不要尝试运行测试。",
        "",
        "## 需求",
        request.strip(),
        "",
        "## 仓库现状（受版本控制的文件清单）",
        facts,
        "",
        "## 队列中尚未完成的任务（不要重复造一张同样的卡）",
    ]
    if existing_titles:
        lines.extend(f"- {title}" for title in existing_titles)
    else:
        lines.append("- （无）")
    if completed_titles:
        lines += ["", "## 已经完成的任务（这些能力已经存在，不要重做）"]
        lines.extend(f"- {title}" for title in completed_titles)
    if agents_text:
        lines += ["", "## 项目约定（AGENTS.md）", agents_text]
    lines += [
        "",
        "## 输出要求",
        '只输出一个 JSON 对象：{"cards": [...]}，不要 markdown 围栏，不要额外解释。',
        "每张卡字段：",
        "- key：计划内唯一短标识（T1、T2…），blocked_by 用它引用",
        "- title：一句话说明做什么",
        "- description：范围、边界、明确不要做的事",
        "- acceptance：可执行的验收标准，必须是具体命令（如 python3 -m unittest discover -s tests）"
        "或可观察行为（如“接口返回 401”），禁止写“功能正常”这类无法判定的话",
        "- blocked_by：可选，字符串数组，写必须先完成的 key",
        f"最多 {MAX_CARDS} 张卡。每张卡只做一件事，能在一次会话内改完 1-3 个文件。",
        "拆分原则：先有能验证的骨架，再有依赖它的改动；不要为了凑数拆出空壳卡。",
    ]
    return "\n".join(lines)


def _flatten_text(value, sep):
    """模型经常把一条标准拆成数组；按分隔符拼回字符串，不丢信息。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sep.join(item.strip() for item in value if item.strip())
    return value


def _pick_key(raw, index):
    for field in ("key", "id"):
        value = raw.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"T{index}"


def normalize_plan(payload, existing_ids=()):
    """把模型输出转成 cards.parse_cards 能接受的卡片，返回 (cards, warnings)。"""
    cards = payload.get("cards") if isinstance(payload, dict) else None
    if isinstance(cards, list) and not cards:
        raise PlanError(
            "规划器没有产出任何卡片。若这些事已经做完，不用再建卡；"
            "若确实需要改动，把需求写得更具体后重试。"
        )
    if not isinstance(cards, list):
        raise PlanError("规划输出缺少 cards 数组")
    if len(cards) > MAX_CARDS:
        raise PlanError(f"规划输出了 {len(cards)} 张卡，超过上限 {MAX_CARDS}")

    existing = set(existing_ids)
    key_to_id = {}
    keys = []
    for index, raw in enumerate(cards, start=1):
        if not isinstance(raw, dict):
            raise PlanError(f"第 {index} 张卡必须是对象")
        key = _pick_key(raw, index)
        if not cards_module.ID_PATTERN.fullmatch(key):
            raise PlanError(
                f"第 {index} 张卡的 key 只能包含字母、数字、下划线与连字符：{key!r}"
            )
        if key in key_to_id:
            raise PlanError(f"第 {index} 张卡的 key 与前面的卡重复：{key}")
        key_to_id[key] = uuid.uuid4().hex
        keys.append(key)

    warnings = []
    rewritten = []
    for index, raw in enumerate(cards, start=1):
        key = keys[index - 1]
        dropped = sorted(
            set(raw) - set(PLANNER_FIELDS) - {"key", "id", "blocked_by"}
        )
        if dropped:
            warnings.append(f"第 {index} 张卡忽略了未知字段：{', '.join(dropped)}")

        item = {field: raw[field] for field in PLANNER_FIELDS if field in raw}
        item["id"] = key_to_id[key]
        for field, sep in (("acceptance", "；"), ("description", "\n"), ("title", " ")):
            if field in item:
                item[field] = _flatten_text(item[field], sep)

        dependencies = raw.get("blocked_by") or []
        if not isinstance(dependencies, list):
            raise PlanError(f"第 {index} 张卡的 blocked_by 必须是数组")
        mapped = []
        for dependency in dependencies:
            if not isinstance(dependency, str) or not dependency.strip():
                raise PlanError(f"第 {index} 张卡的 blocked_by 元素必须是非空字符串")
            dependency = dependency.strip()
            if dependency in key_to_id:
                mapped_id = key_to_id[dependency]
            elif dependency in existing:
                mapped_id = dependency
            else:
                raise PlanError(
                    f"第 {index} 张卡依赖了计划里没有的 key 或任务：{dependency}"
                )
            if mapped_id not in mapped:
                mapped.append(mapped_id)
        item["blocked_by"] = mapped
        rewritten.append(item)

    return cards_module.parse_cards(rewritten, existing_ids=existing_ids), warnings


class Planner:
    """调用只读 codex 进程产出计划草稿；不写数据库。"""

    name = "codex-planner"

    def __init__(
        self,
        executable="codex",
        project_root=None,
        schema_path=PLAN_SCHEMA_PATH,
        model=None,
        env=None,
    ):
        self.executable = [executable] if isinstance(executable, str) else list(executable)
        self.project_root = project_root
        self.schema_path = schema_path
        self.model = model
        self.env = dict(os.environ) if env is None else {**os.environ, **env}

    def build_command(self, worktree_path, out_path, prompt):
        command = [
            *self.executable,
            "exec",
            "-C",
            worktree_path,
            "-s",
            "read-only",
            "--json",
            "-o",
            out_path,
            "--output-schema",
            self.schema_path,
        ]
        if self.model:
            command.extend(["-m", self.model])
        command.append(prompt)
        return command

    def plan(
        self,
        request,
        out_path,
        raw_path,
        timeout_seconds,
        existing_ids=(),
        existing_titles=(),
        completed_titles=(),
    ):
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        jsonl_path = os.path.splitext(raw_path)[0] + ".jsonl"
        stderr_path = os.path.splitext(raw_path)[0] + ".stderr.log"

        facts = collect_facts(self.project_root)
        prompt = build_plan_prompt(
            request, facts, existing_titles, _read_agents(self.project_root), completed_titles
        )
        command = self.build_command(self.project_root, raw_path, prompt)
        outcome = run_process(command, jsonl_path, stderr_path, timeout_seconds, env=self.env, cwd=self.project_root)
        if outcome.timed_out:
            raise PlanError(f"规划超时（{timeout_seconds} 秒），可调大 config.json 的 plan.timeout_seconds")
        if outcome.exit_code != 0:
            raise PlanError(f"规划进程退出码 {outcome.exit_code}，详见 {stderr_path}")
        if not os.path.isfile(raw_path):
            raise PlanError(f"规划进程没有写出结果：{raw_path}")

        with open(raw_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        payload = find_json_object(text, lambda item: isinstance(item.get("cards"), list))
        if payload is None:
            raise PlanError(f"规划输出里找不到 cards 数组，原始输出见 {raw_path}")

        cards, warnings = normalize_plan(payload, existing_ids=existing_ids)
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump({"cards": cards}, handle, ensure_ascii=False, indent=2)
        return {
            "cards": cards,
            "draft_path": out_path,
            "raw_path": raw_path,
            "warnings": warnings,
            "session_id": first_session_id(jsonl_path),
            "stderr_path": stderr_path,
        }
