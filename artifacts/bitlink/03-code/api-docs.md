# API 接口文档

## 变更概览

新增本机只读看板 HTTP 接口 4 个：`GET /`、`GET /tasks/<id>`、`GET /api/tasks`、`GET /api/tasks/<id>`。全部为新增，无修改或删除；没有外部调用方，不涉及旧版本兼容。服务由 `bitlink dashboard` 独立进程启动，默认绑定 `127.0.0.1:8787`。每个请求用只读 SQLite（`mode=ro`）连接渲染页面与 JSON，不提供任何写接口；只读连接打开失败直接返回 `500`，不会回退到读写连接。

## 通用约定

- 协议：HTTP/1.1，仅 `GET`。`POST`、`PUT`、`PATCH`、`DELETE` 一律返回 `405`，响应头带 `Allow: GET`。`/api/*` 的 405 响应体为 JSON `{"error": "method not allowed", "allow": "GET"}`；页面路径的 405 为纯文本 `405 Method Not Allowed`。
- 认证：无。服务默认只监听回环地址，靠网络隔离而不是鉴权保护数据；用 `--host 0.0.0.0` 显式开放时 CLI 会打印警告。
- 响应类型：HTML 页面为 `text/html; charset=utf-8`，JSON 接口为 `application/json; charset=utf-8`，错误文本为 `text/plain; charset=utf-8`。
- 编码：UTF-8，正常响应带 `Content-Length`。
- 自动刷新：两个 HTML 页面都含 `<meta http-equiv="refresh" content="5">`，即 5 秒刷新一次。
- 只读实现：服务进程不持有任何读写连接（包括 keepalive）；每个请求用 `file:<path>?mode=ro` 打开，并执行 `PRAGMA query_only=ON` 作为第二道保险。`mode=ro` 打开失败（例如 WAL 的 `-shm` 缺失、文件权限不足）时直接返回 `500` 文本错误，不回退到读写连接。
- 安全：所有来自数据库的文本经 `html.escape` 输出；页面不引用任何外部资源（无外链 CSS/JS/字体）。
- 错误格式不统一是刻意保留的：HTML 页面、未知路径与数据库错误返回纯文本；`/api/tasks` 与 `/api/tasks/<id>` 的 404/405 返回 JSON，便于脚本消费。
- 任务 id：建卡时限定 `[A-Za-z0-9_-]{1,64}`，不含路径分隔符；路径参数需 URL 编码。

## 接口

### GET /

- 用途：任务列表页，展示 id、标题、状态、attempts、最近一次报告摘要、block_reason、updated_at。
- 权限：无。
- 请求：无路径参数、无查询参数、无请求体。
- 响应：`200`，`text/html; charset=utf-8`。表格列固定为 `id`、`标题`、`状态`、`attempts`（显示为 `attempt_count/max_attempts`）、`最近报告摘要`、`block_reason`、`updated_at`；`id` 链到 `/tasks/<id>`；页脚显示任务总数与刷新周期。
- 错误：数据库读取异常返回 `500` 纯文本；非法方法返回 `405`。
- 兼容性：新增页面，无既有调用方。

### GET /tasks/&lt;id&gt;

- 用途：单任务详情页，展示任务卡字段、尝试时间线与日志路径。
- 权限：无。
- 请求：路径参数 `id` 为任务 id（`uuid4().hex` 或建卡时指定的 id），需 URL 编码。
- 响应：`200`，`text/html; charset=utf-8`。任务卡键值表字段：`id`、`标题`、`状态`、`attempts`、`blocked_by`、`block_reason`、`分支`、`worktree`、`创建时间`、`更新时间`、`验收标准`、`描述`；「尝试时间线」表格列为 `#`（attempt_no）、`phase`、`exit`（exec_exit_code）、`报告摘要`（report_summary）、`review verdict`、`review 理由`（review_reason）、`events.jsonl`、`review.jsonl`。
- 错误：任务不存在返回 `404` 纯文本 `404 任务不存在`；数据库异常 `500`；非法方法 `405`。
- 兼容性：新增页面，无既有调用方。

### GET /api/tasks

- 用途：任务列表 JSON，供脚本或后续前端消费。
- 权限：无。
- 请求：无参数。
- 响应：`200`，`application/json; charset=utf-8`，对象含唯一字段 `tasks`，数组按 `updated_at` 倒序。

  ```json
  {
    "tasks": [
      {
        "id": "22739357968e4d24968897cb610fdb3c",
        "title": "冒烟任务",
        "status": "ready",
        "attempt_count": 0,
        "max_attempts": 3,
        "block_reason": null,
        "updated_at": "2026-11-25T15:37:40.215450+00:00",
        "report_summary": null
      }
    ]
  }
  ```

  字段说明：`status` 取 `ready/running/review/done/failed/blocked/cancelled`；`report_summary` 取该任务 attempt_no 最大且非空的报告摘要，没有则为 `null`；`block_reason` 无阻塞时为 `null`。
- 错误：数据库异常 `500` 纯文本；非法方法 `405`，`application/json`，体为 `{"error": "method not allowed", "allow": "GET"}`，响应头带 `Allow: GET`。
- 兼容性：新增接口，无既有调用方。

### GET /api/tasks/&lt;id&gt;

- 用途：单任务详情 JSON，含任务卡全部字段与全部尝试记录。
- 权限：无。
- 请求：路径参数 `id`，需 URL 编码。
- 响应：`200`，`application/json; charset=utf-8`。顶层为 `tasks` 表整行：`id`、`title`、`description`、`acceptance`、`status`、`blocked_by`（JSON 数组字符串）、`attempt_count`、`max_attempts`、`timeout_seconds`、`block_reason`、`branch`、`worktree_path`、`created_at`、`updated_at`；附加字段 `attempts` 为数组，按 `attempt_no` 升序。

  ```json
  {
    "id": "22739357968e4d24968897cb610fdb3c",
    "title": "冒烟任务",
    "description": "只读看板冒烟",
    "acceptance": "列表里能看到这张卡",
    "status": "done",
    "blocked_by": "[]",
    "attempt_count": 1,
    "max_attempts": 3,
    "timeout_seconds": null,
    "block_reason": null,
    "branch": "bitlink/22739357968e4d24968897cb610fdb3c",
    "worktree_path": "C:\\Users\\<user>\\AppData\\Local\\BitLink\\<hash>\\worktrees\\22739357968e4d24968897cb610fdb3c",
    "created_at": "2026-11-25T15:37:40.205000+00:00",
    "updated_at": "2026-11-25T15:39:02.115000+00:00",
    "attempts": [
      {
        "id": 1,
        "task_id": "22739357968e4d24968897cb610fdb3c",
        "attempt_no": 1,
        "phase": "passed",
        "reason": null,
        "exec_exit_code": 0,
        "exec_session_id": "019a...",
        "exec_jsonl_path": "C:\\Users\\<user>\\AppData\\Local\\BitLink\\<hash>\\attempts\\2273...\\1\\events.jsonl",
        "report_path": "C:\\Users\\<user>\\AppData\\Local\\BitLink\\<hash>\\attempts\\2273...\\1\\report.json",
        "report_summary": "补上了 README 说明",
        "review_verdict": "pass",
        "review_reason": "验收标准逐条满足",
        "review_session_id": "019a...",
        "review_jsonl_path": "C:\\Users\\<user>\\AppData\\Local\\BitLink\\<hash>\\attempts\\2273...\\1\\review.jsonl",
        "started_at": "2026-11-25T15:37:41.000000+00:00",
        "ended_at": "2026-11-25T15:39:02.000000+00:00"
      }
    ]
  }
  ```

  字段说明：`phase` 取 `running/completed/failed/timeout/interrupted/rejected/passed`；`review_verdict` 取 `pass/reject` 或 `null`；`exec_session_id` 与 `review_session_id` 来自两次不同 codex 进程的 JSONL 事件，正常情况下不相等。
- 错误：任务不存在返回 `404`，`application/json`，体为 `{"error": "task not found", "id": "<id>"}`；数据库异常 `500` 纯文本；非法方法 `405`，`application/json`，体为 `{"error": "method not allowed", "allow": "GET"}`。
- 兼容性：新增接口，无既有调用方。

## 与测试的对应关系

`tests/integration/test_dashboard.py` 分别请求 `/`、`/tasks/T`、`/api/tasks`、`/api/tasks/T`，并断言 `200`、列表页含 `content="5"`、详情页含 review 理由与日志路径、`POST /` 返回 `405` 纯文本、`POST /api/tasks` 返回 JSON 405、未知路径与未知任务返回 `404`、标题里的 `<script>` 被转义为 `&lt;script&gt;`、页面不含 `http://`/`https://` 外链；`test_readonly_connection_is_query_only` 与 `test_dashboard_open_readonly_rejects_insert` 直接尝试 `INSERT`/`UPDATE`，断言只读连接抛 `sqlite3.OperationalError`；`test_list_api_survives_daemon_closing_database` 断言 daemon 正常关闭数据库后看板仍能返回列表；`test_readonly_open_failure_returns_500_without_write_fallback` 断言只读连接打开失败直接 `500`、不回退读写连接。
