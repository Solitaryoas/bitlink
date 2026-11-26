"""只读看板：标准库 ThreadingHTTPServer + 只读 SQLite 连接。

仅提供 GET；HTML 页面每 5 秒自动刷新，所有数据库文本经 html.escape；
非 GET 返回 405，未知路径 404，页面不引用任何外部资源。
"""

import html
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

from bitlink import db

REFRESH_SECONDS = 5

_LIST_SQL = """
SELECT t.id, t.title, t.status, t.attempt_count, t.max_attempts,
       t.block_reason, t.updated_at,
       (SELECT a.report_summary FROM attempts a
         WHERE a.task_id = t.id AND a.report_summary IS NOT NULL
         ORDER BY a.attempt_no DESC LIMIT 1) AS report_summary
FROM tasks t
WHERE :status IS NULL OR :status = '' OR t.status = :status
ORDER BY t.updated_at DESC, t.created_at
"""

_PAGE_STYLE = (
    "body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;margin:2rem;color:#222}"
    "table{border-collapse:collapse;margin-top:1rem}"
    "th,td{padding:.35rem .8rem;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}"
    "th{background:#fafafa}code{background:#f3f3f3;padding:.1rem .3rem}"
    ".muted{color:#777}"
)


def _open_readonly(data_dir):
    """只读打开数据库；失败由调用方转 500，不回退到读写连接。

    回退会让「只读」只依赖代码自觉，与 api-docs 的只读承诺不符。
    """
    return db.connect(data_dir, readonly=True)


def list_tasks(conn, status=None):
    rows = conn.execute(_LIST_SQL, {"status": status}).fetchall()
    return [dict(row) for row in rows]


def task_detail(conn, task_id):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    detail = dict(row)
    detail["blocked_by"] = db.decode_blocked_by(detail["blocked_by"])
    detail["attempts"] = [
        dict(attempt)
        for attempt in conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no", (task_id,)
        ).fetchall()
    ]
    return detail


def _page(title, body):
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">'
        "<title>BitLink 看板</title>"
        f"<style>{_PAGE_STYLE}</style>"
        f"</head><body><h1>{html.escape(title)}</h1>{body}</body></html>"
    )


def render_list_html(tasks):
    if not tasks:
        body = '<p class="muted">（没有任务）</p>'
    else:
        head = (
            "<tr><th>id</th><th>标题</th><th>状态</th><th>attempts</th>"
            "<th>最近报告摘要</th><th>block_reason</th><th>updated_at</th></tr>"
        )
        rows = []
        for task in tasks:
            task_id = str(task["id"])
            link = quote(task_id, safe="")
            rows.append(
                "<tr>"
                f'<td><a href="/tasks/{link}"><code>{html.escape(task_id[:8])}</code></a></td>'
                f"<td>{html.escape(str(task['title']))}</td>"
                f"<td>{html.escape(str(task['status']))}</td>"
                f"<td>{html.escape(str(task['attempt_count']))}/{html.escape(str(task['max_attempts']))}</td>"
                f"<td>{html.escape(str(task['report_summary'] or ''))}</td>"
                f"<td>{html.escape(str(task['block_reason'] or ''))}</td>"
                f"<td class=\"muted\">{html.escape(str(task['updated_at']))}</td>"
                "</tr>"
            )
        body = f"<table>{head}{''.join(rows)}</table>"
    body += (
        '<p class="muted">'
        f'共 {len(tasks)} 个任务 · 每 {REFRESH_SECONDS} 秒自动刷新 · '
        "只读页面</p>"
    )
    return _page("BitLink 任务列表", body)


def render_detail_html(task):
    card_rows = [
        ("id", task["id"]),
        ("标题", task["title"]),
        ("状态", task["status"]),
        ("attempts", f"{task['attempt_count']}/{task['max_attempts']}"),
        ("blocked_by", json.dumps(task["blocked_by"], ensure_ascii=False)),
        ("block_reason", task["block_reason"] or ""),
        ("分支", task["branch"] or ""),
        ("worktree", task["worktree_path"] or ""),
        ("创建时间", task["created_at"]),
        ("更新时间", task["updated_at"]),
        ("验收标准", task["acceptance"]),
        ("描述", task["description"]),
    ]
    card = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in card_rows
    )

    if task["attempts"]:
        attempt_rows = "".join(
            "<tr>"
            f"<td>#{html.escape(str(attempt['attempt_no']))}</td>"
            f"<td>{html.escape(str(attempt['phase']))}</td>"
            f"<td>{html.escape(str(attempt['exec_exit_code'] or ''))}</td>"
            f"<td>{html.escape(str(attempt['report_summary'] or ''))}</td>"
            f"<td>{html.escape(str(attempt['review_verdict'] or ''))}</td>"
            f"<td>{html.escape(str(attempt['review_reason'] or ''))}</td>"
            f"<td><code>{html.escape(str(attempt['exec_jsonl_path'] or ''))}</code></td>"
            f"<td><code>{html.escape(str(attempt['review_jsonl_path'] or ''))}</code></td>"
            "</tr>"
            for attempt in task["attempts"]
        )
        attempts_table = (
            "<table><tr><th>#</th><th>phase</th><th>exit</th><th>报告摘要</th>"
            "<th>review verdict</th><th>review 理由</th><th>events.jsonl</th>"
            f"<th>review.jsonl</th></tr>{attempt_rows}</table>"
        )
    else:
        attempts_table = '<p class="muted">（还没有尝试记录）</p>'

    body = (
        f"<table>{card}</table>"
        f"<h2>尝试时间线</h2>{attempts_table}"
        '<p class="muted"><a href="/">返回列表</a></p>'
    )
    return _page(f"任务 {task['id'][:8]}", body)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "BitLinkDashboard/0.1"

    def log_message(self, format, *args):  # 保持测试与 CLI 输出干净
        return

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/":
                return self._render_list()
            if path == "/api/tasks":
                return self._render_list_json()
            if path.startswith("/api/tasks/"):
                return self._render_detail_json(path[len("/api/tasks/") :])
            if path.startswith("/tasks/"):
                return self._render_detail(path[len("/tasks/") :])
            return self._send_text(404, "404 Not Found")
        except sqlite3.Error as exc:
            return self._send_text(500, f"数据库错误：{exc}")

    def do_POST(self):
        self._method_not_allowed()

    def do_PUT(self):
        self._method_not_allowed()

    def do_PATCH(self):
        self._method_not_allowed()

    def do_DELETE(self):
        self._method_not_allowed()

    # --- renderers ------------------------------------------------------

    def _render_list(self):
        conn = _open_readonly(self.server.data_dir)
        try:
            tasks = list_tasks(conn)
        finally:
            conn.close()
        self._send(200, "text/html; charset=utf-8", render_list_html(tasks))

    def _render_list_json(self):
        conn = _open_readonly(self.server.data_dir)
        try:
            tasks = list_tasks(conn)
        finally:
            conn.close()
        self._send_json(200, {"tasks": tasks})

    def _render_detail(self, task_id):
        conn = _open_readonly(self.server.data_dir)
        try:
            task = task_detail(conn, task_id)
        finally:
            conn.close()
        if task is None:
            return self._send_text(404, "404 任务不存在")
        self._send(200, "text/html; charset=utf-8", render_detail_html(task))

    def _render_detail_json(self, task_id):
        conn = _open_readonly(self.server.data_dir)
        try:
            task = task_detail(conn, task_id)
        finally:
            conn.close()
        if task is None:
            return self._send_json(404, {"error": "task not found", "id": task_id})
        self._send_json(200, task)

    # --- transport ------------------------------------------------------

    def _method_not_allowed(self):
        path = unquote(urlparse(self.path).path)
        if path.startswith("/api/"):
            self._send_json(
                405,
                {"error": "method not allowed", "allow": "GET"},
                extra_headers={"Allow": "GET"},
            )
            return
        self._send(
            405,
            "text/plain; charset=utf-8",
            "405 Method Not Allowed",
            extra_headers={"Allow": "GET"},
        )

    def _send_text(self, status, text):
        self._send(status, "text/plain; charset=utf-8", text)

    def _send_json(self, status, payload, extra_headers=None):
        self._send(
            status,
            "application/json; charset=utf-8",
            json.dumps(payload, ensure_ascii=False),
            extra_headers=extra_headers,
        )

    def _send(self, status, content_type, body, extra_headers=None):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, data_dir):
        self.data_dir = data_dir
        # 看板进程不持有任何读写连接（包括 keepalive）：所有请求走 mode=ro。
        # WAL 的 -shm 缺失等原因导致只读打开失败时，请求直接 500，不回退。
        super().__init__(address, DashboardHandler)


def make_server(data_dir, host="127.0.0.1", port=8787):
    return DashboardServer((host, port), data_dir)


def serve(data_dir, host="127.0.0.1", port=8787):
    server = make_server(data_dir, host, port)
    actual_host, actual_port = server.server_address[0], server.server_address[1]
    if actual_host not in ("127.0.0.1", "localhost", "::1"):
        print(f"警告：看板绑定在 {actual_host}，同网段其他机器可以访问只读数据")
    print(f"看板已启动：http://{actual_host}:{actual_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("看板已停止")
    finally:
        server.server_close()
