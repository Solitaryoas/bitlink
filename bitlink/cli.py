"""命令行入口：init/add/list/show/cancel/retry/approve/reject/daemon/dashboard。

退出码统一为：0 成功、2 输入非法、3 不存在或环境不满足、4 状态不允许当前操作。
"""

import argparse
import json
import os
import shlex
import sys
import time

from bitlink import cards as cards_module
from bitlink import config as config_module
from bitlink import db
from bitlink import planner as planner_module

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_ENV = 3
EXIT_STATE = 4

COMMANDS = (
    "init",
    "add",
    "plan",
    "list",
    "show",
    "cancel",
    "retry",
    "approve",
    "reject",
    "daemon",
    "dashboard",
)

CANCELLABLE = [
    db.STATUS_READY,
    db.STATUS_RUNNING,
    db.STATUS_REVIEW,
    db.STATUS_FAILED,
    db.STATUS_BLOCKED,
]
RETRYABLE = [db.STATUS_FAILED, db.STATUS_BLOCKED, db.STATUS_REVIEW]
REJECTABLE = [
    db.STATUS_READY,
    db.STATUS_BLOCKED,
    db.STATUS_FAILED,
    db.STATUS_REVIEW,
]


class CliError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _add_common(parser, suppress_defaults=False):
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument("--project", default=default, help="项目路径，默认当前目录")
    parser.add_argument("--data-dir", default=default, help="数据目录，默认放在项目之外")
    parser.add_argument(
        "--json",
        dest="json",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="以 JSON 输出",
    )


def build_parser():
    parser = argparse.ArgumentParser(prog="bitlink", description="本地任务编排器")
    _add_common(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in COMMANDS:
        sub = subparsers.add_parser(name)
        _add_common(sub, suppress_defaults=True)
        if name == "add":
            sub.add_argument("--file", required=True, help="任务卡 JSON 文件")
        if name == "plan":
            sub.add_argument("--request", default=None, help="需求文本")
            sub.add_argument("--file", default=None, help="从文件读取需求文本")
            sub.add_argument("--out", default=None, help="草稿输出路径，默认写进 <data-dir>/plans/")
            sub.add_argument(
                "--apply",
                action="store_true",
                help="校验通过后直接入队；不加时只出草稿，不动数据库",
            )
        if name == "list":
            sub.add_argument("--status", default=None)
        if name in ("show", "cancel", "retry", "approve"):
            sub.add_argument("task_id")
        if name == "approve":
            sub.add_argument("--note", default=None, help="人工放行备注，写进事件")
        if name == "reject":
            sub.add_argument("task_id")
            sub.add_argument("--reason", default="")
        if name == "daemon":
            sub.add_argument("--concurrency", type=int, default=None)
        if name == "dashboard":
            sub.add_argument("--host", default=None)
            sub.add_argument("--port", type=int, default=None)
    return parser


def _is_json(args):
    return bool(getattr(args, "json", False))


def _print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _open_repository(args, require_init=True):
    config = config_module.load_config(args.project, args.data_dir)
    data_dir = config["data_dir"]
    if require_init and not os.path.exists(db.db_path(data_dir)):
        raise CliError(
            EXIT_ENV, f"数据目录尚未初始化，请先运行 bitlink init（{data_dir}）"
        )
    repo = db.Repository(db.connect(data_dir))
    db.migrate(repo.conn)
    return config, repo


def _require_task(repo, task_id):
    task = repo.get_task(task_id)
    if task is None:
        raise CliError(EXIT_ENV, f"任务不存在：{task_id}")
    return task


# --- commands ----------------------------------------------------------


def cmd_init(args):
    try:
        project_root = config_module.git_toplevel(args.project or os.getcwd())
        base_ref = config_module.git_head(project_root)
    except config_module.ConfigError as exc:
        raise CliError(EXIT_ENV, str(exc)) from exc

    data_dir = (
        os.path.abspath(args.data_dir)
        if args.data_dir
        else config_module.default_data_dir(project_root)
    )
    os.makedirs(data_dir, exist_ok=True)

    repo = db.Repository(db.connect(data_dir))
    try:
        db.migrate(repo.conn)
        repo.set_meta("project_root", project_root)
        repo.set_meta("base_ref", base_ref)
    finally:
        repo.close()

    payload = {"project_root": project_root, "data_dir": data_dir, "base_ref": base_ref}
    if _is_json(args):
        _print_json(payload)
    else:
        print(f"已初始化\n项目: {project_root}\n数据目录: {data_dir}\nbase_ref: {base_ref}")
    return EXIT_OK


def cmd_add(args):
    config, repo = _open_repository(args)
    try:
        existing = {task["id"] for task in repo.list_tasks()}
        parsed = cards_module.load_cards_file(args.file, existing_ids=existing)
        ids = repo.create_cards(parsed)
    finally:
        repo.close()
    if _is_json(args):
        _print_json({"ids": ids})
    else:
        for task_id in ids:
            print(task_id)
    return EXIT_OK


def _codex_command():
    """规划器用的 codex 可执行文件；BITLINK_CODEX 可覆盖（JSON 数组或命令字符串）。"""
    raw = os.environ.get("BITLINK_CODEX")
    if not raw:
        return "codex"
    if raw.strip().startswith("["):
        return json.loads(raw)
    return shlex.split(raw)


def cmd_plan(args):
    """把一段需求拆成任务卡草稿；默认只写文件，--apply 才入队。"""
    if args.request and args.file:
        raise CliError(EXIT_INPUT, "--request 与 --file 只能选一个")
    request = args.request
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8-sig") as handle:
                request = handle.read()
        except OSError as exc:
            raise CliError(EXIT_ENV, f"无法读取需求文件：{args.file}（{exc}）") from exc
    if not request or not request.strip():
        raise CliError(EXIT_INPUT, "缺少需求文本：用 --request 或 --file 提供")

    config, repo = _open_repository(args)
    try:
        existing = repo.list_tasks()
    finally:
        repo.close()
    existing_ids = {task["id"] for task in existing}
    existing_titles = [task["title"] for task in existing]

    plan_config = config.get("plan") or {}
    planner = planner_module.Planner(
        executable=_codex_command(),
        project_root=config["project_root"],
        model=plan_config.get("model"),
    )
    plans_dir = os.path.join(config["data_dir"], "plans")
    out_path = args.out or os.path.join(plans_dir, f"{time.strftime('%Y%m%d-%H%M%S')}.json")
    raw_path = os.path.splitext(out_path)[0] + ".raw.json"

    result = planner.plan(
        request,
        out_path,
        raw_path,
        plan_config.get("timeout_seconds") or 300,
        existing_ids=existing_ids,
        existing_titles=existing_titles,
    )
    cards = result["cards"]

    ids = None
    if args.apply:
        config, repo = _open_repository(args)
        try:
            ids = repo.create_cards(cards)
        finally:
            repo.close()

    if _is_json(args):
        payload = {
            "draft_path": result["draft_path"],
            "raw_path": result["raw_path"],
            "warnings": result["warnings"],
            "cards": cards,
        }
        if ids is not None:
            payload["ids"] = ids
        _print_json(payload)
        return EXIT_OK

    print(f"计划草稿：{result['draft_path']}")
    print(f"{'id':<10}{'依赖':<20}title")
    for card in cards:
        print(f"{card['id'][:8]:<10}{str(len(card['blocked_by'])) + ' 个':<20}{card['title']}")
    for warning in result["warnings"]:
        print(f"注意：{warning}")
    if ids is None:
        print(f"\n确认无误后入队：bitlink add --file \"{result['draft_path']}\"")
        print("或直接重跑并加 --apply。")
    else:
        print("\n已入队：" + " ".join(ids))
    return EXIT_OK


def cmd_list(args):
    config, repo = _open_repository(args)
    try:
        rows = repo.list_tasks(args.status)
    finally:
        repo.close()
    if _is_json(args):
        _print_json(rows)
        return EXIT_OK
    if not rows:
        print("（没有任务）")
        return EXIT_OK
    print(f"{'id':<10}{'status':<12}{'attempts':<10}title")
    for row in rows:
        print(
            f"{row['id'][:8]:<10}{row['status']:<12}"
            f"{str(row['attempt_count']) + '/' + str(row['max_attempts']):<10}{row['title']}"
        )
    return EXIT_OK


def cmd_show(args):
    config, repo = _open_repository(args)
    try:
        task = repo.show_task(args.task_id)
    finally:
        repo.close()
    if task is None:
        raise CliError(EXIT_ENV, f"任务不存在：{args.task_id}")
    if _is_json(args):
        _print_json(task)
        return EXIT_OK

    attempts = task["attempts"]
    print(f"id: {task['id']}")
    print(f"title: {task['title']}")
    print(f"status: {task['status']}")
    print(f"attempts: {task['attempt_count']}/{task['max_attempts']}")
    print(f"blocked_by: {task['blocked_by']}")
    print(f"branch: {task['branch']}")
    print(f"worktree: {task['worktree_path']}")
    print(f"block_reason: {task['block_reason']}")
    print(f"created_at: {task['created_at']}")
    print(f"updated_at: {task['updated_at']}")
    print(f"acceptance: {task['acceptance']}")
    print(f"description: {task['description']}")
    print("尝试记录:")
    if not attempts:
        print("  （无）")
    for attempt in attempts:
        print(
            f"  #{attempt['attempt_no']} phase={attempt['phase']} "
            f"exit={attempt['exec_exit_code']} reason={attempt['reason']}"
        )
        print(f"    exec_session={attempt['exec_session_id']} review_session={attempt['review_session_id']}")
        print(f"    events.jsonl={attempt['exec_jsonl_path']}")
        print(f"    report.json={attempt['report_path']}")
        print(f"    review.json={attempt['review_jsonl_path']}")
        print(f"    review_verdict={attempt['review_verdict']} review_reason={attempt['review_reason']}")
    return EXIT_OK


def _manual_transition(args, expect, to, event_type, payload=None, **fields):
    config, repo = _open_repository(args)
    try:
        task = _require_task(repo, args.task_id)
        changed = repo.transition(args.task_id, expect, to, **fields)
        if not changed:
            raise CliError(
                EXIT_STATE,
                f"任务当前状态为 {task['status']}，不允许 {event_type}",
            )
        repo.record_event(args.task_id, "human", event_type, payload)
    finally:
        repo.close()
    return EXIT_OK


def cmd_cancel(args):
    return _manual_transition(
        args, CANCELLABLE, db.STATUS_CANCELLED, "cancel", block_reason=None
    )


def cmd_retry(args):
    return _manual_transition(
        args, RETRYABLE, db.STATUS_READY, "retry", payload=None, block_reason=None
    )


def cmd_approve(args):
    payload = {"note": args.note} if args.note else None
    return _manual_transition(
        args,
        [db.STATUS_REVIEW],
        db.STATUS_DONE,
        "approve",
        payload=payload,
        block_reason=None,
    )


def cmd_reject(args):
    reason = args.reason or ""
    return _manual_transition(
        args,
        REJECTABLE,
        db.STATUS_READY,
        "reject",
        payload={"reason": reason},
        block_reason=None,
    )


def cmd_daemon(args):
    from bitlink import daemon as daemon_module

    config = config_module.load_config(args.project, args.data_dir)
    data_dir = config["data_dir"]
    if not os.path.exists(db.db_path(data_dir)):
        raise CliError(EXIT_ENV, f"数据目录尚未初始化，请先运行 bitlink init（{data_dir}）")
    if args.concurrency:
        config["concurrency"] = args.concurrency
    instance = daemon_module.Daemon(config)
    try:
        instance.run()
    except daemon_module.AlreadyRunning as exc:
        raise CliError(EXIT_ENV, str(exc)) from exc
    return EXIT_OK


def cmd_dashboard(args):
    from bitlink import dashboard as dashboard_module

    config = config_module.load_config(args.project, args.data_dir)
    data_dir = config["data_dir"]
    if not os.path.exists(db.db_path(data_dir)):
        raise CliError(EXIT_ENV, f"数据目录尚未初始化，请先运行 bitlink init（{data_dir}）")
    host = args.host or "127.0.0.1"
    port = args.port or config["dashboard"]["port"]
    try:
        dashboard_module.serve(data_dir, host=host, port=port)
    except OSError as exc:
        raise CliError(EXIT_ENV, f"看板启动失败：{exc}") from exc
    return EXIT_OK


HANDLERS = {
    "init": cmd_init,
    "add": cmd_add,
    "plan": cmd_plan,
    "list": cmd_list,
    "show": cmd_show,
    "cancel": cmd_cancel,
    "retry": cmd_retry,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "daemon": cmd_daemon,
    "dashboard": cmd_dashboard,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return HANDLERS[args.command](args)
    except CliError as exc:
        print(f"错误：{exc.message}", file=sys.stderr)
        return exc.code
    except config_module.ConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ENV
    except cards_module.CardError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    except planner_module.PlanError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
