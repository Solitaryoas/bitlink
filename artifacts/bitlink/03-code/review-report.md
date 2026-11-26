<!-- devflow-template: review-report@1.0 -->
# 代码审查报告

## 审查结论

结论：通过（无阻断问题，可放行）。本轮 P0 0 条、P1 0 条、P2 4 条。

上一轮 2 条 P1 与 6 条 P2 全部验收为已修复。两个此前被证伪的点已用独立探针复验，均得到预期结果：`db.connect(readonly=True)` 执行 `INSERT`/`UPDATE` 抛 `sqlite3.OperationalError` 且未落库，看板渲染路径只剩 `mode=ro` 连接；运行标记里是真实子进程 pid，进程存活时 `ensure_worktree` 抛 `worktree_in_use`，杀进程后可复用并清除标记。全量测试实跑 112 条全部通过。

剩余 4 条 P2 属于窄窗口、恢复策略或文档举例，不影响放行。最值得处理的两条：恢复时遇到存活标记直接把任务判为失败状态（只读 review 孤儿也被当成写占用），以及 daemon 占位 pid 到子进程 pid 覆盖之间的毫秒级窗口。

## 审查范围

- diff 清单：`git diff --stat` 共 40 个文件、6378 行新增，全为新增文件。核心是 `bitlink/` 14 个模块、2 个 JSON Schema、`bitlink.cmd`、`tests/` 下 8 个测试文件与 3 个 helper，另有 6 份 devflow 文档与 `workflow-state.json`。
- 逐条复核上一轮 8 个问题（2 条 P1 + 6 条 P2），见下节。
- 独立复验：只读连接是否真的不可写、看板是否残留读写连接；worktree 标记是否记录真实子进程 pid、判活与清理是否正确。
- 回归新代码：`running.py` 写入/覆盖/删除时序、`transaction()` 捕获 `BaseException` 后连接是否可用、`_drain_futures` 能否终止真实子进程、取消窄窗口清理是否可能误删别的任务目录。
- API 契约：按 `03-code/api-docs.md` 核对 `dashboard.py` 的 405/404、内容类型、只读语义与错误格式。
- 实跑：全量 unittest、`python3 -m bitlink --help`，以及 5 段独立探针（只读写、worktree 占用、WAL 异常退出、中断收尾、事务回滚）。

## 上一轮 8 条逐条验收

| 编号 | 结论 | 代码位置 | 证据 |
| --- | --- | --- | --- |
| P1-R1 worktree 占用检测 | 已修复 | `worktree.py:42-62`、`running.py:24-112`、`procs.py:127-143`、`daemon.py:285-301` | 标记写 `<data-dir>/running/<task_id>.json`；`procs.run_process` 在 Popen 后用子进程 pid 覆盖（`procs.py:129-133`），退出只清自己那条；daemon 先写占位 pid（`daemon.py:296`），复用前按 pid 判活。探针：child pid 46476 ≠ daemon pid 5428，`REUSE_WHILE_ALIVE: RAISED worktree_in_use`，杀进程后 `REUSE_AFTER_KILL True`、`MARKER_CLEARED True`。残留窄窗口见新 P2-2 |
| P1-R2 看板只读连接 | 已修复 | `db.py:131-145`、`dashboard.py:36-41`、`dashboard.py:267-275` | `file:...?mode=ro` + `PRAGMA query_only=ON`；`DashboardServer` 不再持有任何连接。探针：`QUERY_ONLY 1`，INSERT/UPDATE 均 `OperationalError`，`PERSISTED_PROBE 0`、`TASK_TITLE t`；全仓 grep 确认看板路径只有 `db.connect(..., readonly=True)` |
| P2-A 405 文档 | 已修复 | `api-docs.md:64`、`dashboard.py:229-243` | 文档已改为 JSON 405，与 `/api/*` 分支一致；`test_api_method_not_allowed_returns_json` 断言响应体与 `Allow: GET` |
| P2-B 只读测试 | 已修复 | `test_dashboard.py:145-163` | `test_readonly_connection_is_query_only`、`test_dashboard_open_readonly_rejects_insert` 直接执行 INSERT/UPDATE 断言异常并回查未落库；原 500 mock 用例保留为附加 |
| P2-C 中断事务回滚 | 已修复 | `db.py:183-194` | `transaction()` 捕获 `BaseException` 后 ROLLBACK 并原样抛出。探针：事务内抛 KeyboardInterrupt 后标题回滚为 `t`，紧接着 `recover_running()` 返回 `[('T','ready')]`，下一个事务仍可提交（`NEXT_TXN_OK ok`）。`BEGIN IMMEDIATE` 调用到进入 `try` 之间仍是理论极小窗口，未构造用例 |
| P2-D 终态错位 | 已修复 | `daemon.py:379-402`、`daemon.py:451-495` | pass 与 reject 的 attempt 终态和任务迁移同事务；两个注入 `KeyboardInterrupt` 的用例断言 attempt 为 interrupted、`review_verdict` 为 None、任务回 ready。`_fail` 路径仍是两段写入，见新 P2-3 |
| P2-E submit 窗口 | 已修复 | `daemon.py:216-243` | submit 与等待同处一个 `except KeyboardInterrupt`，收尾走 `_drain_futures`。测试断言 `_stop` 与 `terminate_all` 被调用；我用真实子进程探针验证：中断落在第二次 submit，`_drain_futures` 0.84 秒结束，子进程退出码 1，registry 清空 |
| P2-F 取消窄窗口 | 已修复 | `daemon.py:303-318`、`daemon.py:523-545` | transition 落空且状态为 cancelled 时带刚建路径调 `_handle_cancelled`；`test_cancel_between_worktree_creation_and_transition_removes_worktree` 断言目录删除、分支保留。删除路径由 `<data>/worktrees/<task_id>` 派生，未见跨任务误删 |

## 问题清单

### 新 P2-1 恢复时遇到存活标记一律把任务判 failed，只读 review 孤儿也按写占用处理

- 位置：`bitlink/worktree.py:42-50`（pid 存活抛 `worktree_in_use`）、`bitlink/daemon.py:284-294`（直接 `_fail`）。
- 触发：daemon 被强杀而 codex 子进程因 `CREATE_NEW_PROCESS_GROUP` 存活。重启后 `recover_running` 把任务回 ready，下一轮 `_run_attempt` 命中存活标记，任务被写成 `failed`（`block_reason=worktree_in_use`），`claim_ready` 刚加过的尝试预算照常消耗。
- 影响：确实避免了两个写进程并发（上一轮 P1 的目标），但任务落在 `failed` 而非等待或 blocked。用户在孤儿退出前 `retry` 会再消耗一次尝试；连续硬杀可把任务推到 blocked。review 阶段标记的是只读 review 进程，按「写占用」处理偏严。
- 证据：`test_live_running_marker_marks_task_worktree_in_use` 断言 status=failed、block_reason=worktree_in_use；探针在存活子进程下 `REUSE_WHILE_ALIVE: RAISED worktree_in_use`。
- 建议：命中存活标记时保持 ready 等待孤儿退出，或转 blocked 并记录 pid；沿用 failed 时至少把 pid 与 `started_at` 写进 `block_reason`，并区分 exec 与 review 两种标记。

### 新 P2-2 占位 pid 到子进程 pid 的覆盖窗口

- 位置：`bitlink/daemon.py:296`（写 daemon pid 占位）、`bitlink/procs.py:118-133`（Popen 之后才写子进程 pid）。
- 触发：daemon 恰在 `Popen` 返回、`write_marker` 执行前被强杀。此时标记仍是已死的 daemon pid，子进程已存活；重启时 pid 判死、清标记、`git status` 返回 0，目录被复用。
- 影响：即上一轮 P1-R1 想消除的「两个 codex 同改一个目录」，但窗口从整个执行期缩到毫秒级，需要外部强杀恰好命中。未实测，按代码路径推得。
- 建议：写标记前先落一个「即将启动子进程」的状态，恢复时对 `started_at` 很近且 pid 已死的标记保守等待一轮；更彻底是让子进程自己持 worktree 文件锁。

### 新 P2-3 `_fail` 的 attempt 终态与任务迁移仍是两段写入

- 位置：`bitlink/daemon.py:425-449`。
- 触发：`update_attempt(phase=failed/timeout, ended_at=...)` 已提交，进程在随后 `transition(running/review→failed)` 之前被杀。
- 影响：`recover_running`（`db.py:354-372`）只把 `ended_at IS NULL` 的 attempt 标 interrupted，这条不会被动；任务按预算回 ready 重跑，看板出现「attempt 已 failed（超时/执行失败）但任务又 ready」。与 P2-D 修掉的错位同类、方向相反，超时任务会被多跑一次。
- 证据：按代码路径推得，未构造中断用例。
- 建议：把 `_fail` 的两步也包进 `transaction()`，与 pass/reject 保持同一写法。

### 新 P2-4 api-docs 的只读打开失败举例与实测不符

- 位置：`artifacts/bitlink/03-code/api-docs.md:14`（「例如 WAL 的 `-shm` 缺失、文件权限不足」）。
- 影响：实测保留 `-wal`、删掉 `-shm` 后，`mode=ro` 仍能打开并读到 2 行（目录可写时 SQLite 会重建 `-shm`）。按文档排障会误判原因。只影响文档举例，不影响只读语义本身。
- 建议：举例改为数据目录不可写、库文件不存在等实测会失败的场景，或注明 `-shm` 缺失只在目录不可写时才会失败。

## 严重级别

| 级别 | 数量 | 条目 |
| --- | --- | --- |
| P0 | 0 | 无 |
| P1 | 0 | 无 |
| P2 | 4 | 新 P2-1 存活标记一律 failed；新 P2-2 占位到子进程 pid 覆盖窗口；新 P2-3 `_fail` 两段写入；新 P2-4 文档举例不符 |

整体判定：通过。上一轮 8 条全部闭合，本轮无 P0/P1；4 条 P2 都是窄窗口或运维/文档问题，不构成发布阻断。

## 证据

### 必跑命令的实际输出

```text
> python3 -m unittest discover -s tests -v
Ran 112 tests in 116.948s
OK
（退出码 0；实跑 112 条，change-report 第三轮写的是 111 条，差 1 条，不影响结论）

> python3 -m bitlink --help
usage: bitlink [-h] [--project PROJECT] [--data-dir DATA_DIR] [--json]
               {init,add,list,show,cancel,retry,approve,reject,daemon,dashboard} ...
EXIT=0（10 个子命令齐备）
```

结果划分：自动化测试 112 条 passed、0 failed；真实 `codex exec` 端到端 not-run；真实 Ctrl+C 信号送达 not-run；`taskkill /F /T` 对真实 codex 进程树 not-run。

### 独立探针 1：只读连接是否真的不可写

```text
> python3 bl_probe_ro.py（PYTHONPATH 指向项目根）
QUERY_ONLY 1
SELECT_OK 1
INSERT_ON_READONLY: REJECTED OperationalError attempt to write a readonly database
SELECT_WITH_WRITER_OPEN 1
UPDATE_ON_READONLY: REJECTED OperationalError
PERSISTED_PROBE 0
TASK_TITLE t
=> 只读连接不可写、未落库；读写连接打开时只读仍可读（WAL 场景）
```

看板代码路径核对：`dashboard.py` 里所有 4 个渲染入口都走 `_open_readonly` → `db.connect(data_dir, readonly=True)`；`DashboardServer.__init__` 不打开任何连接，全仓无 keepalive 读写连接。

### 独立探针 2：worktree 占用检测

```text
> python3 bl_probe_wt.py
WORKTREE ...\data\worktrees\task1
CHILD_PID 46476
DAEMON_PID 5428
CHILD_PID_NE_DAEMON True
MARKER_PID 46476
MARKER_PID_IS_CHILD True
PID_ALIVE True
REUSE_WHILE_ALIVE: RAISED worktree_in_use
PID_ALIVE_AFTER_KILL False
REUSE_AFTER_KILL True
MARKER_CLEARED True
NON_WORKTREE_DIR: RAISED worktree_in_use
=> 标记写的是真实子进程 pid；存活拦截、退出后复用并清理均符合预期
```

### 回归探针 3：提交窗口中断后仍能终止真实子进程

```text
> python3 bl_probe_drain.py
TICK: raised KeyboardInterrupt as expected
ELAPSED_SEC 0.84
CHILD_PIDS [47124]
ALL_CHILDREN_EXITED True
EXIT_CODES [1]
REGISTRY_EMPTY True
DAEMON_STOP_FLAG True
=> `_drain_futures` 在第二次 submit 注入中断时 terminate 了已登记的子进程，未卡满超时
```

### 回归探针 4：事务中断后连接可用

```text
> python3 bl_probe_txn.py
INTERRUPTED True
TITLE_AFTER_ROLLBACK t
RECOVER_RESULT [('T', 'ready')]
STATUS_AFTER_RECOVER ready
NEXT_TXN_OK ok
```

### 回归探针 5：非正常退出后的只读打开

```text
> python3 bl_probe_wal.py
WAL_EXISTS True
SHM_EXISTS True
RO_ROWS 2
RO_AFTER_UNCLEAN: OK

> python3 bl_probe_wal2.py（保留 -wal、删除 -shm）
SHM_REMOVED True WAL_EXISTS True
RO_ROWS 2
RO_WITHOUT_SHM: OK
=> 本机上 -shm 缺失不会导致只读打开失败（新 P2-4 的依据）
```

### API 文档与实现核对

`api-docs.md:5/9/14/64/114` 与 `dashboard.py` 一致：`/api/*` 的 405 为 JSON 且带 `Allow: GET`，页面路径 405 为纯文本；`/api/tasks/<id>` 未知任务 404 JSON，`/tasks/<id>` 404 纯文本；数据库错误 500 纯文本；响应都带 `Content-Length`；页面 `content="5"`。只读语义描述（`mode=ro` + `query_only`、无读写连接）与代码一致，只有 `:14` 的失败举例与实测不符（新 P2-4）。

### 新增测试是否真的复现原问题

- `test_readonly_connection_is_query_only` / `test_dashboard_open_readonly_rejects_insert`：旧实现 `file:<path>` 默认读写，INSERT 会成功，断言直接红。
- `test_reuse_is_rejected_while_running_marker_pid_is_alive`、`test_run_process_records_child_pid_in_marker`：旧 `ensure_worktree`/`run_process` 没有 marker 参数，调用即 TypeError。
- `test_transaction_rolls_back_on_keyboard_interrupt`：旧实现只有 `_transaction` 且只捕 `Exception`，调用 `transaction()` 即 AttributeError。
- `test_interrupt_between_pass_write_and_done_transition_leaves_no_mismatch`、`test_interrupt_between_reject_write_and_ready_transition_leaves_no_mismatch`：旧代码先提交 attempt 终态再迁移，恢复后 attempt 仍是 passed/rejected，断言红。
- `test_interrupt_during_submit_still_stops_and_terminates_children`：旧 `tick` 的 submit 循环在 try 外，`terminate_all` 不会被调用，断言红；该用例只断言调用，真实终止由上面的探针补齐。
- `test_cancel_between_worktree_creation_and_transition_removes_worktree`：旧代码 transition 落空直接返回，目录残留，断言红。

### 未验证项

- 真实 `codex exec` 端到端：not-run（费用原因）；`--output-schema` 在 Windows 带空格路径下的行为、codex 退出码语义仍只有帮助层证据。
- 真实 Ctrl+C 在 Windows 上送达主线程：not-run；自动化用 `Daemon.stop()` 与伪造线程池覆盖同一路径。第二次 Ctrl+C 落在 `_drain_futures` 收尾时的行为仍未实测。
- `taskkill /F /T /PID` 能否杀净真实 codex 派生进程树：not-run；探针只到「结束 Python 子进程后 daemon 快速返回」。
- 新 P2-2 的毫秒级覆盖窗口：未实测，按代码路径推得。
- 不同用户/权限导致 `mode=ro` 打开失败：只有 mock 覆盖 500 路径。

### 关键引用

- `bitlink/daemon.py:140-175`（run 与 lock）、`:177-200`（stop 与取消监督线程）、`:204-243`（tick 与 `_drain_futures`）、`:281-301`（ensure 与标记占位/清理）、`:303-318`（取消窄窗口）、`:379-402`（pass 事务）、`:419-449`（`_fail`）、`:451-495`（`_apply_rejection`）、`:523-545`（`_handle_cancelled`）
- `bitlink/db.py:131-151`（连接）、`:183-194`（transaction）、`:338-388`（recover_running）
- `bitlink/running.py:16-112`、`bitlink/procs.py:15-149`、`bitlink/worktree.py:32-101`
- `bitlink/dashboard.py:36-41`、`:162-175`、`:229-243`、`:267-275`
- `bitlink/executors/codex.py:78-106`、`bitlink/review.py:107-138`
- `tests/integration/test_daemon.py:380-491`、`tests/integration/test_dashboard.py:137-177`、`tests/integration/test_worktree.py:94-146`、`tests/unit/test_repository.py:176-187`
- `artifacts/bitlink/03-code/api-docs.md:5,9,14,64,114`
