# 变更报告

## 变更摘要

新增一个零第三方依赖的 Python 3 包 `bitlink`：CLI（init/add/list/show/cancel/retry/approve/reject/daemon/dashboard）、SQLite 状态机、git worktree 隔离、codex 执行器、独立只读 review、daemon 调度循环与本机只读看板。运行数据（数据库、worktree、日志、锁、config.json）全部写在项目目录之外，默认 `%LOCALAPPDATA%\BitLink\<sha1(项目绝对路径)[:12]>`。

验收标准落点：AC1 建卡与列表、AC2 依赖门控、AC3 worktree 隔离且主工作区干净、AC4 结构化报告与空 evidence 判失败、AC5 review 独立只读且会话不同、AC6 打回三次熔断、AC7 崩溃恢复与单实例锁、AC8 看板列表/详情/刷新/转义、AC9 超时、AC10 人工命令与 daemon 不覆盖。

## 修改文件

全部为新增文件，未修改 `artifacts/bitlink/01-requirement/`、`02-design/` 与 `workflow-state.json`。

| 路径 | 目的 | 对应任务 |
| --- | --- | --- |
| `bitlink/__init__.py` | 版本号 | T1 |
| `bitlink/__main__.py` | `python3 -m bitlink` 入口，转发 `cli.main` 并落退出码 | T1 |
| `bitlink/config.py` | git 仓库校验、数据目录哈希推导、config.json 与默认值深合并 | T1 |
| `bitlink/db.py` | 连接（WAL/外键/busy_timeout）、幂等迁移、`Repository.transition`/`claim_ready`/`recover_running` 等 | T2 |
| `bitlink/cards.py` | 卡字段校验、同批次与库内依赖校验、自引用与环检测、整批拒绝 | T3 |
| `bitlink/cli.py` | 10 个子命令与统一退出码 0/2/3/4，人工命令写 events（actor=human） | T4 |
| `bitlink/procs.py` | 子进程执行、超时 taskkill→terminate→kill、JSONL 会话 id 解析（执行器与 review 共用） | T5 |
| `bitlink/executors/base.py` | `ExecResult` 与 `Executor` 协议 | T5 |
| `bitlink/executors/codex.py` | `codex exec -C ... -s workspace-write --json -o ... --output-schema ...`，提示词注入任务卡与 AGENTS.md 绝对路径 | T5 |
| `bitlink/executors/__init__.py` | 执行器子包 | T5 |
| `bitlink/schemas/report.schema.json` | 报告结构（status/summary/evidence/files_changed/blockers，evidence 允许空数组） | T5 |
| `bitlink/schemas/review.schema.json` | review 结构（verdict/reason/findings） | T5 |
| `bitlink/worktree.py` | `ensure_worktree`/`remove_worktree`/`make_diff`，分支 `bitlink/<id>` | T6 |
| `bitlink/review.py` | `-s read-only` 独立 review 进程、verdict 解析、异常即 reject | T7 |
| `bitlink/daemon.py` | 文件锁、崩溃恢复、依赖门控派发、超时与阶段推进、Ctrl+C 收敛 | T8 |
| `bitlink/dashboard.py` | `ThreadingHTTPServer` 只读看板，4 个 GET 接口、5 秒刷新、html.escape | T9 |
| `bitlink.cmd` | Windows 包装，设置 PYTHONPATH 后调用 `python3 -m bitlink` | T1 |
| `tests/__init__.py`、`tests/unit/__init__.py`、`tests/integration/__init__.py`、`tests/helpers/__init__.py` | 测试包结构 | T10 |
| `tests/helpers/temp_repo.py` | 临时 git 仓库（init + 初始提交） | T10 |
| `tests/helpers/fake_executor.py` | 不调用 codex 的假执行器（可配置报告、睡眠、退出码、回调） | T10 |
| `tests/helpers/fake_reviewer.py` | 按预设 verdict 返回的假 reviewer | T10 |
| `tests/unit/test_repository.py` | 迁移版本、批量建卡、非法迁移、并发 transition、依赖门控、attempt 计数、恢复 | T2 |
| `tests/unit/test_cards.py` | 合法单卡、批量互引、未知依赖、自引用、成环、缺 acceptance、类型、BOM | T3 |
| `tests/unit/test_codex_executor.py` | 参数拼装、stdout 落 events.jsonl、会话解析、超时、非零退出、两份 schema 字段 | T5 |
| `tests/unit/test_review.py` | read-only 沙箱、review/exec 会话不同、reject、非法 JSON、缺输出、非法 verdict | T7 |
| `tests/integration/test_cli.py` | 真实子进程跑 CLI，覆盖 AC1 与 AC10 命令面、退出码 2/3/4 | T4 |
| `tests/integration/test_worktree.py` | worktree 建/复用/删除、主仓库 status 干净、diff 生成 | T6 |
| `tests/integration/test_daemon.py` | AC2/AC4/AC6/AC7/AC9/AC10 与 worktree 绑定、双会话落库 | T8 |
| `tests/integration/test_dashboard.py` | 4 个接口、5 秒刷新、405、404、转义、无外链 | T9 |
| `artifacts/bitlink/03-code/api-docs.md` | 看板 HTTP 接口文档 | T11 |

## 验证结果

### passed

| 命令 | 结果 |
| --- | --- |
| `python3 -m unittest discover -s tests -v` | `Ran 77 tests in 65.3s` → `OK`；输出中可见 `test_ac1_` 到 `test_ac10_` 命名的方法（AC5/AC7/AC8/AC10 各有多条） |
| `python3 -m bitlink --help` | 退出码 0，列出 init/add/list/show/cancel/retry/approve/reject/daemon/dashboard 共 10 个子命令 |
| `.\bitlink.cmd --help` | 退出码 0，同上 |
| `python3 -c "from bitlink.config import load_config; print(load_config())"` | 打印合并配置：concurrency=2、poll_interval_seconds=2、timeout_seconds=1800、max_attempts=3、exec/review.model=None、dashboard.port=8787，以及 project_root 与 data_dir |
| 手工冒烟（临时 git 仓库，非自动化）：`bitlink init` → `bitlink add --file cards.json --json` → `bitlink dashboard --port 8791` → `GET /api/tasks` / `GET /` / `POST /` | init 返回项目路径、数据目录与 base_ref；add 返回任务 id；`/api/tasks` 返回含该任务的 JSON；`/` 含任务标题且含 `content="5"`；`POST /` 返回 405 |
| 手工冒烟（临时 git 仓库，非自动化）：`bitlink daemon` 启动后立刻再启动一个 `bitlink daemon` | 第一个实例保持运行；第二个实例退出码 3，stderr 为「已有 daemon 实例持有锁：…\daemon.lock」；`daemon.lock` 文件存在 |
| `git --version` / `python3 --version` / `sqlite3.sqlite_version` / `codex --version` | git 2.56.1.windows.1、Python 3.14.0、SQLite 3.50.4、codex-cli 0.163.0-alpha.11.2 |

### failed

无。

### not-run

- 真实 `codex exec` 端到端（会产生账号费用，按要求记为 not-run）。因此 `--output-schema` 在 Windows 带空格路径下的行为、codex 退出码语义、模型是否按 schema 写报告，只有假执行器与 CLI 帮助层面的证据。
- 真实超时：`taskkill /F /T /PID` 对 codex 派生进程树的终止效果未实测；自动化只覆盖到假执行器返回 `timed_out=True` 与 `procs.terminate_process` 的调用路径。

### 用户手工验证步骤（真实 codex，需登录且产生费用）

```powershell
cd <一个有多提交的 git 仓库>
python3 -m bitlink init
# cards.json 写入一张只检查工作区状态的卡：
# [{"title":"冒烟","description":"只运行 git status 并给出输出","acceptance":"报告里含 git status 的输出"}]
python3 -m bitlink add --file cards.json
python3 -m bitlink daemon          # 终端 A，保持运行
python3 -m bitlink dashboard       # 终端 B，打开 http://127.0.0.1:8787/
python3 -m bitlink show <id>       # 终端 C，看 attempts 与日志路径
```

预期观察：`%LOCALAPPDATA%\BitLink\<hash>\attempts\<id>\1\` 下出现 `events.jsonl`、`stderr.log`、`report.json`、`diff.patch`、`review.jsonl`、`review.json`；`git status --porcelain` 在主仓库为空；看板 attempts 从 0 变 1 且 5 秒内反映状态；`exec_session_id` 与 `review_session_id` 不相等；不通过时 `block_reason` 可从 `bitlink show` 看到。

## 计划偏差

1. 新增 `bitlink/procs.py`（计划未列）：执行器与 review 都需要「超时终止 + JSONL 会话 id 解析」，抽公共模块避免两份复制。
2. 任务卡接受可选 `id`（计划只写 `uuid4().hex`）：计划要求「批量互引」校验，但自动生成的 id 无法在输入里互相引用。未提供 id 时仍生成 uuid4；提供时校验同批次不重复、库内不存在。
3. `make_diff` 用 `git add -A` + `git diff --cached <base_ref>`，而非计划中的 `git diff <base_ref>...HEAD`：codex 通常不在 worktree 里提交，后者会得到空补丁，review 无证据可看。改后同时覆盖已提交与未提交改动。
4. 看板保留一个读写连接以维持 WAL 的 `-shm` 文件，每个请求再用 `mode=ro` 打开：Windows 上 WAL 库在没有任何连接时，单独的只读连接可能打不开。只读语义不变，所有查询走只读连接。
5. 读卡片文件与 `config.json` 用 `utf-8-sig`：冒烟时 PowerShell `Set-Content -Encoding utf8` 写入的 BOM 让 JSON 解析失败，容错后 Windows 用户手工建卡不会被 BOM 卡住。
6. daemon 每轮用 `ThreadPoolExecutor` 并发执行本轮认领的任务，并等本轮结束再进入下一轮：并发上限语义与计划一致，同时让「一轮」的状态可断言，避免测试里出现竞态。
7. `daemon` 子命令只加了 `--concurrency` 覆盖，未实现计划外的迭代次数开关；自动化测试直接构造 `Daemon(max_iterations=N)`。
8. 人工 `reject` 允许从 `ready/blocked/failed` 回到 `ready`，理由写入 `events`（actor=human, type=reject），不写 `block_reason`：ready 状态不应携带阻塞原因，否则看板与调度语义混乱。

## API 接口文档

已生成：`03-code/api-docs.md`（4 个 GET 接口：`/`、`/tasks/<id>`、`/api/tasks`、`/api/tasks/<id>`，含字段、示例、错误与兼容性说明）。

## 剩余风险

- 真实 codex 行为未验证（not-run）：报告缺失、schema 不合法、evidence 为空都会判 `failed` 并由人工 `retry`，不会污染 review 或静默成功，但首次真实运行可能需要调整提示词措辞。
- `taskkill /F /T /PID` 对 codex 进程树的效果只做了软路径验证；若树未杀净，下一轮 worktree 可能被占用，当前实现会因 `git worktree` 冲突把任务判 `failed` 并保留日志。
- 看板只读依赖同进程的 keepalive 连接；当数据目录权限变化或 daemon/看板由不同用户启动时，只读连接可能失败并返回 500。
- 单实例锁是文件锁，只在同一台机器、同一用户语义下可靠；共享数据目录的多用户场景不在保证范围。
- 状态机只允许 `cancelled` 由人工触发，超时与空 evidence 直接 `failed` 且不自动重试（与 AC4/AC9 措辞一致）；如果用户期望自动重试执行失败，需要改调度策略。
- worktree 与分支在 `done/blocked/failed` 时保留，磁盘占用随尝试次数增长，首版没有自动清理策略。
- 执行阶段是 `workspace-write` 沙箱，模型仍可修改 worktree 内文件；提示词禁止 push/合并 main 只是约束，不是技术阻断（review 只读沙箱只保护 review 阶段）。

## 第二轮修复（REVIEW 打回后，2026-11-25）

对应 `03-code/review-report.md` 的 2 条 P1 与 6 条 P2。全部按红—绿推进：先补失败用例，再改实现。

### P1-1 review 阶段失败收敛

- `bitlink/daemon.py`：`_fail` 的期望状态集改为 `[running, review]`，transition 返回 False 时写 `failed_transition_missed` 事件（含当前状态），不再静默丢弃；review 的 `make_diff` + `reviewer.review` + 回写 attempt 包进 try/except，`OSError`、SQLite 锁超时、`PermissionError` 统一走 `_apply_rejection`，attempt 记 `phase=rejected`、`review_verdict=reject`、`review_reason=review_error: ...`，再按尝试预算回 ready 或 blocked。打回逻辑抽成 `_apply_rejection`，正常 reject 与异常 reject 共用。
- `bitlink/db.py`：`recover_running` 查询条件改为 `status IN ('running','review')`，最新的未结束 attempt 标 `interrupted`（reason `daemon_restarted`），`attempt_count < max_attempts` 回 ready，否则 blocked + `interrupted_exhausted`；`recovered` 事件带 `from` 字段。
- `bitlink/cli.py`：`RETRYABLE` 与 `REJECTABLE` 加入 `review`，人工出口不再只有 `approve`。
- 测试：`test_recover_review_task_requeues_within_budget`、`test_recover_review_task_blocks_when_budget_exhausted`、`test_restart_recovers_task_stuck_in_review`、`test_review_start_failure_converges_to_reject`、`test_fail_converges_from_review_status`、`test_fail_records_event_when_transition_misses`、`test_reject_review_task_returns_ready`、`test_retry_review_task_returns_ready`。

### P1-2 Ctrl+C 与人工 cancel 终止子进程

- `bitlink/procs.py`：新增 `ProcessRegistry`（按任务 id 登记 Popen，支持 `terminate(key)` 与 `terminate_all()`）；`run_process` 增加 `registry`/`registry_key` 参数，Popen 后登记、`finally` 注销。
- `bitlink/executors/codex.py`、`bitlink/review.py`：构造器接收 `registry`，调用 `run_process` 时以 task id 登记，执行与 review 进程都能被终止。
- `bitlink/daemon.py`：Daemon 自建 registry 并注入默认执行器与 reviewer；`stop()` 置标志并 `registry.terminate_all()`；`tick` 捕获 KeyboardInterrupt 时先 `stop()` 再等 futures，避免 `ThreadPoolExecutor.__exit__` 的 `wait=True` 卡满一个超时周期；新增 `_monitor_cancellations` 监督线程（0.5 秒轮询数据库，发现任务 `cancelled` 就终止对应进程），CLI 与 daemon 无需直接通信；`_aborted` 在每次阶段推进前检查取消与停止，中断的 attempt 标 `interrupted`，任务按预算回 ready 或 blocked（`_abort_attempt`）。
- `bitlink/worktree.py`：`ensure_worktree` 复用已有目录前先跑 `git -C <path> status --porcelain`，失败抛 `WorktreeError(reason='worktree_in_use')`；daemon 直接用它作 `block_reason`，避免两个 codex 同改一个目录。
- 测试：`test_cancel_terminates_running_subprocess_and_skips_review`（真实子进程睡 30 秒，取消后 daemon 15 秒内返回且 attempt 为 interrupted）、`test_stop_terminates_registered_subprocess`、`test_worktree_in_use_marks_task_failed`、`test_existing_directory_that_is_not_a_worktree_is_reported_in_use`。

### P2

| 条目 | 改动 | 测试 |
| --- | --- | --- |
| P2-1 取消清理 worktree | `daemon._handle_cancelled` 标记 interrupted 后调 `worktree.remove_worktree`，分支保留，`interrupted` 事件带 `worktree_removed` | `test_cancelled_task_removes_worktree_and_keeps_branch` |
| P2-2 看板连接只读 | `dashboard._open_readonly` 去掉读写回退，只读打开失败由 `do_GET` 的 `sqlite3.Error` 分支返回 500；keepalive 连接保留并写明原因 | `test_readonly_open_failure_returns_500_without_write_fallback` |
| P2-3 id 校验 | `cards.py` 用 `[A-Za-z0-9_-]{1,64}` 全匹配，`..`、`/`、`\`、超长一律整批拒绝 | `test_id_with_path_separators_or_traversal_is_rejected`、`test_add_card_with_path_like_id_returns_2_and_writes_nothing` |
| P2-4 405 与文档一致 | `/api/*` 的 405 返回 JSON `{"error": "method not allowed", "allow": "GET"}`，页面路径仍纯文本；`api-docs.md` 同步 | `test_api_method_not_allowed_returns_json` |
| P2-5 approve 备注 | `approve` 增加可选 `--note`，写进 `approve` 事件 payload | `test_approve_with_note_records_note_in_event_payload` |
| P2-6 三条缺失用例 | 报告文件缺失、依赖 id 不存在、review 可执行文件不存在 | `test_ac4_missing_report_file_fails`、`test_dependency_on_missing_task_marks_blocked`、`test_review_start_failure_converges_to_reject` |

### 实际命令与输出

```text
> python3 -m unittest discover -s tests -v
Ran 98 tests in 93.357s
OK
（退出码 0；上一轮 77 条，本轮新增 21 条）

> python3 -m bitlink --help
EXIT=0（10 个子命令齐备）

> python3 -m bitlink approve --help
EXIT=0，options 中出现 `--note NOTE  人工放行备注，写进事件`
```

### 仍未验证

- 真实 `codex exec` 端到端仍未运行（费用与凭据原因，not-run），包括 `review` 启动失败在真实 codex 路径下的表现。
- Windows `taskkill /F /T /PID` 能否杀净 codex 派生的整棵进程树：自动化只验证到「结束 Python 子进程后 daemon 快速返回」，没有验证真实 codex 进程树。
- 真实 Ctrl+C 的信号送达未实测：Windows 上主线程阻塞在 `future.result()` 时 `_thread.interrupt_main()` 不会立即送达（写过一个模拟用例，实测跑满 30 秒，已删除）。自动化用 `Daemon.stop()` 覆盖同一条「置停止标志 + terminate_all」路径；第二次 Ctrl+C 落在线程池收尾阶段的行为同样未实测。
- 看板 keepalive 连接在「只读打开失败」场景下的实际表现由 mock 覆盖，未用不同用户/权限变化实测。

## 第三轮修复（REVIEW 第二轮打回后，2026-11-25）

对应 `03-code/review-report.md` 的 2 条 P1 与 6 条 P2。全部按红—绿推进：先补失败用例，再改实现。

### P1-R1 worktree 占用检测识别存活进程

- 新增 `bitlink/running.py`：运行标记写在 `<data-dir>/running/<task_id>.json`（pid、task_id、started_at），不写进 worktree；`pid_is_alive` 在 Windows 用 `tasklist /FI "PID eq <pid>" /NH`（不可用时退回 ctypes `OpenProcess`），POSIX 用 `os.kill(pid, 0)`。`clear_marker(pid=...)` 只删属于指定进程的标记，避免后写入者被前一个进程误删。
- 标记里的 pid 是**真正的写进程**：daemon 派发时先写自己的 pid 占位；`procs.run_process` 新增 `marker_dir`/`marker_task_id`，Popen 后用子进程 pid 覆盖标记，退出时只清自己那条。daemon 被强杀而 codex 存活时，留下的就是 codex 的 pid，复用前能判出来。`CodexExecutor`/`CodexReviewer` 接收 `data_dir` 并向下传递。
- `bitlink/worktree.py`：`ensure_worktree` 增加 `data_dir` 参数（缺省取 worktrees 目录的父目录）；复用前先查标记，pid 存活抛 `WorktreeError(reason="worktree_in_use")`，pid 已死或标记损坏则清理后复用；原有「目录存在但不是 worktree」判定保留。
- `bitlink/daemon.py`：`_run_attempt` 在 `ensure_worktree` 成功后写标记，`finally` 删除；尝试主体抽到 `_execute_attempt`，成功/失败/取消/中断都走同一清理路径。
- 测试：`test_reuse_is_rejected_while_running_marker_pid_is_alive`（真实存活子进程）、`test_reuse_after_marker_writer_exits_clears_marker`、`test_stale_marker_is_cleared_before_reuse`、`test_live_running_marker_marks_task_worktree_in_use`、`test_running_marker_exists_during_attempt_and_is_removed_after`、`test_run_process_records_child_pid_in_marker`（断言标记 pid 是子进程而非 daemon，且退出后清理）。

### P1-R2 看板只读连接（含 P2-B）

- `bitlink/db.py`：`connect(readonly=True)` 改为 `file:<path>?mode=ro`（`uri=True`）并执行 `PRAGMA query_only=ON`。
- `bitlink/dashboard.py`：删除 `DashboardServer` 的读写 keepalive 连接；看板进程不再持有任何读写连接。`mode=ro` 打开失败由 `do_GET` 的 `sqlite3.Error` 分支返回 500 文本，不回退。
- 测试：`test_readonly_connection_is_query_only`（`PRAGMA query_only` 为 1）、`test_dashboard_open_readonly_rejects_insert`（真实 `INSERT`/`UPDATE` 断言 `sqlite3.OperationalError`，并确认未落库）、`test_list_api_survives_daemon_closing_database`（daemon 关闭数据库后列表与页面仍 200）；保留 `test_readonly_open_failure_returns_500_without_write_fallback` 作为附加用例。

### P2

| 条目 | 改动 | 测试 |
| --- | --- | --- |
| P2-A 405 文档 | `api-docs.md` 的 `/api/tasks` 405 改为 JSON；删除 keepalive 描述，改为不持有读写连接 + `mode=ro`/`query_only` + 打开失败 500；错误格式与测试对应关系同步 | 无新增（文档） |
| P2-B 只读测试 | 与 P1-R2 合并，直接断言写操作失败，原 500 用例保留为附加 | 同上 |
| P2-C 中断事务 | `Repository._transaction` 改为公开 `transaction()`，捕获 `BaseException` 回滚后原样抛出 | `test_transaction_rolls_back_on_keyboard_interrupt` |
| P2-D 终态错位 | pass 的 review 字段/`phase=passed`/任务迁移收进同一事务；`_apply_rejection` 的 rejected 终态与 ready/blocked 迁移收进同一事务 | `test_interrupt_between_pass_write_and_done_transition_leaves_no_mismatch`、`test_interrupt_between_reject_write_and_ready_transition_leaves_no_mismatch` |
| P2-E submit 窗口 | `tick` 增量提交 future，submit 与等待同处一个 `except KeyboardInterrupt`，收尾抽到 `_drain_futures` | `test_interrupt_during_submit_still_stops_and_terminates_children` |
| P2-F 取消窄窗口 | transition 落空且已 cancelled 时，用刚建的路径调 `_handle_cancelled(worktree_path=...)`，立即删目录、保留分支 | `test_cancel_between_worktree_creation_and_transition_removes_worktree` |

### 实际命令与输出

```text
> python3 -m unittest discover -s tests -v
Ran 111 tests in 114.896s
OK
（退出码 0；上一轮 98 条，本轮新增 13 条）

> python3 -m bitlink --help
EXIT=0（10 个子命令齐备）

> python3 validate_artifacts.py ... --path 03-code/change-report.md
见下方自检结果
```

### 仍未验证

- 真实 `codex exec` 端到端仍未运行（费用原因，not-run）。存活标记判活针对「daemon 被强杀后残留 codex」的路径：自动化用真实 Python 子进程写入 pid 验证判活与清理，真实 codex 进程树下的 pid 行为未实测。
- 真实 Ctrl+C 信号送达未实测；P2-E 用假线程池在 submit 阶段注入 `KeyboardInterrupt`，验证的是兜底代码路径。
- Windows `taskkill /F /T /PID` 能否杀净真实 codex 进程树仍未验证。
- `mode=ro` 在「daemon 正常关闭数据库」后可用已由自动化验证（SQLite 3.50.4）；不同用户/权限导致只读打开失败时，只有 mock 覆盖 500 路径。



## 交付后补充（2026-11-26）：真实端到端验证与集成修正

DevFlow 流程完成后，在临时 git 仓库里用真实 `codex exec` 跑了完整链路。三次运行暴露了假执行器覆盖不到的环境问题，均已修正。

### 运行结果

- 最终一次：任务 `running → review → done`，`review_verdict=pass`；`diff.patch` 127 字节（非空）；执行会话 `01a213cb-d889-7491-856c-1b2d70e3ff7a` 与 review 会话 `01a213cc-0b27-7b10-adb1-4b8fe6750a07` 不同；主工作区除测试用的 `cards.json` 外保持干净，分支 `bitlink/<id>` 留在仓库里。

### 发现与修正

1. **沙箱禁读 `.git`**：codex 在 worktree 里执行 `git status` 得到 `fatal: gitfile does not point to a valid repository`，随后进入长达 7 分钟的自证循环，任务一直停在 running。
   - 修正：执行提示明确「不要执行任何 git 命令，版本控制由 BitLink 负责」，不再要求 agent 读工作区外的 `AGENTS.md`，改为把内容内联（上限 8000 字符）。
2. **`--output-schema` 不生效**：当前模型（`qoder-cn/dfmodel`）把最终消息写成散文，`-o report.json` 拿到的是说明文字，daemon 判 `report_invalid`。
   - 修正：新增 `bitlink/jsonout.py` 从散文或 markdown 围栏里提取 JSON；提示要求同时在**工作区内**写 `.bitlink-report.json` 作为兜底，执行器读走后删除，避免污染 diff；报告支持 `changes`/`verification` 别名并规范化后写回磁盘。
3. **沙箱 ACL 把 agent 产物封在另一个用户下**：Windows 上 `git add -A` 报 `Permission denied`，diff 恒为空，人工也无法合并。
   - 实测对比：`features.windows_sandbox_service=false`、`stdin=DEVNULL`、`CREATE_NEW_PROCESS_GROUP` 三种组合都会复现；只有 `--dangerously-bypass-approvals-and-sandbox` 产出的文件可读。
   - 修正：新增配置 `exec.windows_sandbox`（默认 `bypass`）与 `review.windows_sandbox`（默认 `service`）。exec 用 bypass 换取可读产物，daemon 启动时打印警告；review 不改文件，保留沙箱让 `-s read-only` 真正生效。

### 安全取舍（需要知会）

exec 默认 `bypass` 意味着执行 agent 拥有当前用户的完整权限，隔离边界只有 git worktree，不再有 OS 级沙箱。这是在本机让工具可用的唯一可行路径：保留沙箱时 daemon 读不到 agent 写出的文件，diff、review 与人工合并全部失效。要恢复沙箱可把 `config.json` 的 `exec.windows_sandbox` 设为 `service`，代价是上面第 3 条会重现。

### 验证

```text
python3 -m unittest discover -s tests -v
Ran 130 tests in 127.277s
OK
```

新增用例覆盖：散文/围栏 JSON 提取、别名规范化、工作区兜底报告、提示内联 AGENTS.md、Windows bypass 参数、review 不使用 bypass。

### 仍未验证

- 真实 Ctrl+C 信号送达与 `taskkill /F /T` 杀树。
- 大改动时 diff 内联会被截断到 60000 字符，截断后的 review 质量未验证。
- 换用支持结构化输出的模型时 `--output-schema` 是否恢复生效未验证。

### 复审修正（同日）

独立复审对上述改动提出 3 条 P1，已修：

1. **兜底报告被误删**：`-o` 输出合法 JSON 但不是报告（回显任务卡、`{"ok":true}`）时，原逻辑会删掉正确的 `.bitlink-report.json`。现改为按报告契约判定，契约抽到 `bitlink/report.py` 供 daemon 与执行器共用。
2. **兜底报告跨尝试残留**：daemon 在写入与采纳之间被强杀时，残留文件会被下一次尝试直接当成本次结果。现在 `executor.run` 开头先删工作区内的残留兜底文件。
3. **bypass 默认不设防且泄漏内部路径**：改为 fail-closed——Windows 上必须显式在 `<data-dir>/config.json` 写 `{"exec": {"windows_sandbox": "bypass"}}`，否则 daemon 退出码 3 并给出说明；提示里不再暴露 `attempt_dir` 路径；review 启动前删除已存在的 `review.json`，防止执行 agent 预写伪造 pass。

同时修了 P2：status 只接受 `completed/failed/blocked`；空白 evidence 项按空处理；JSON 提取改为扫描全部对象取第一份符合契约的（避免先回显任务卡时取错）；review 提示要求「看到不可读或截断必须 reject」；补齐对应用例。

验证：

```text
python3 -m unittest discover -s tests -v
Ran 137 tests in 129.508s
OK (skipped=1)
```

真实端到端复跑（临时仓库）：未写 config 时 daemon 退出码 3；写入 bypass 后 `running → review → done`，`diff.patch` 155 字节，执行会话 `01a213d9…` 与 review 会话 `01a213da…` 不同，主工作区干净。
