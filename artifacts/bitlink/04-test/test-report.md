<!-- devflow-template: test-report@1.1 -->
# 测试报告

## 测试范围

覆盖需求报告 AC1–AC10 与设计「验证策略」列出的高风险项：依赖成环与同批次互引、`claim_ready` 并发抢占、超时/中断/取消三条路径的 worktree 与 attempt 终态、看板 405/404、HTML 转义与只读写失败、CLI 退出码 0/2/3/4 每个分支、报告缺失/非法/evidence 为空三种失败判定。

被测对象是 `bitlink/` 全部模块（CLI、SQLite 状态机、cards 校验、daemon 调度、worktree、执行器与 review 适配、看板）。本次只补测试，未改生产代码。

## 项目测试能力

仓库只有标准库 `unittest` 三层，无 pytest、无浏览器 E2E、无 CI 配置：

- 单元（`tests/unit/`，4 个文件）：`test_repository.py` 状态迁移、事务、`claim_ready`；`test_cards.py` 卡片校验与环检测；`test_codex_executor.py` 命令拼装、超时、JSONL 会话解析、两份 schema；`test_review.py` 只读沙箱、verdict 解析、异常转 reject。
- 集成（`tests/integration/`，4 个文件）：`test_cli.py` 用 `subprocess` 跑真实 `python3 -m bitlink`；`test_daemon.py` 在临时 git 仓库里用真 SQLite + `FakeExecutor`/`FakeReviewer` 跑真实调度；`test_worktree.py` 真 `git worktree`；`test_dashboard.py` 真 `ThreadingHTTPServer` + `http.client`。
- 夹具（`tests/helpers/`）：`temp_repo.py`（git init + 初始提交）、`fake_executor.py`（按脚本写报告/JSONL，可配睡眠、退出码、缺失报告）、`fake_reviewer.py`（预设 verdict）。

## 风险与覆盖矩阵

| 验收标准 / 风险 | 测试层级 | 用例或命令 | 结果 |
| --- | --- | --- | --- |
| AC1 建卡：`add` 退出码 0、库内 status=ready、`list` 可见 | CLI 集成 | `integration.test_cli.CliTest.test_ac1_add_card_exit_zero_and_list_json_shows_it` | passed |
| AC1「看板可见」 | HTTP 集成 | `test_ac8_json_list_api`、`test_ac8_list_page_shows_task_and_auto_refresh`（同一 `tasks` 读取路径） | passed |
| AC2 依赖未完成不派发、完成后下一轮派发 | 集成 + 单元 | `test_ac2_dependent_task_waits_for_prerequisite`（执行顺序断言为 `["B","A"]`）、`unit.test_repository.test_claim_ready_skips_unmet_dependency` | passed |
| AC2 依赖已失败则任务转 blocked | 集成 | `test_blocked_dependency_marks_task_blocked`、`test_dependency_on_missing_task_marks_blocked`（block_reason 精确到 `dependency:B:failed` / `dependency:ghost:missing`） | passed |
| AC3 独立 worktree 与分支、主工作区 `git status` 干净 | 集成 | `test_ac3_worktree_is_isolated_and_main_worktree_stays_clean`、`test_worktree_is_created_per_task`（daemon 落库 branch=`bitlink/T`） | passed |
| AC4 报告五字段、evidence 为空判 failed 且不进 review | 集成 + 单元 | `test_ac4_empty_evidence_fails_without_review`（reviewer 未被调用）、`unit.test_codex_executor.test_report_schema_is_parseable_and_requires_fields` | passed |
| AC4 报告缺失 / 非法 JSON / 非零退出 | 集成 | `test_ac4_missing_report_file_fails`、`test_ac4_invalid_report_fails`、`unit.test_codex_executor.test_non_zero_exit_code_is_propagated` | passed |
| AC5 review 是独立进程、只读沙箱、写入数据库 | 单元 + 集成 | `test_ac5_review_uses_read_only_sandbox`（argv 含 `-s read-only`、不含 `workspace-write`）、`test_ac5_review_session_differs_from_execution_session`、`test_workflow_records_distinct_exec_and_review_sessions`（两个 session id 与两份 JSONL 路径都不同） | passed |
| AC5 review 异常不把任务卡在 review | 单元 + 集成 | `test_invalid_json_becomes_reject_with_readable_reason`、`test_missing_output_becomes_reject`、`test_review_start_failure_converges_to_reject` | passed |
| AC6 打回回 ready、attempts 累加、第 3 次 blocked 且理由可查 | 集成 | `test_ac6_reject_three_times_blocks_task`（attempt_count=3、block_reason 含理由、末次 attempt phase=rejected） | passed |
| AC7 重启后不双跑、单实例锁 | 集成 + 单元 + CLI | `test_ac7_restart_recovers_running_task_without_double_execution`、`test_restart_recovers_task_stuck_in_review`、`test_ac7_second_daemon_is_rejected_by_lock`、`unit.test_repository.test_recover_running_requeues_within_budget`、**新增** `test_daemon_with_held_lock_returns_3` | passed |
| AC8 列表/详情/5 秒刷新/转义/只读 | HTTP 集成 | `test_ac8_list_page_shows_task_and_auto_refresh`（`content="5"`）、`test_ac8_detail_page_shows_review_reason_and_logs`、`test_ac8_json_detail_api`、`test_ac8_script_in_title_is_escaped`（`&lt;script&gt;`） | passed |
| AC8 405/404 与只读连接写失败 | HTTP 集成 + 单元 | `test_post_is_method_not_allowed`、**新增** `test_put_patch_delete_return_405`、`test_api_method_not_allowed_returns_json`、`test_unknown_path_returns_404`、`test_unknown_task_returns_404`、`test_readonly_connection_is_query_only`、`test_dashboard_open_readonly_rejects_insert`、`test_readonly_open_failure_returns_500_without_write_fallback` | passed |
| AC9 超时不进 review，worktree 与分支保留 | 集成 + 单元 | `test_ac9_timeout_fails_without_review`（本次补断言：worktree 目录仍在、`bitlink/T` 分支仍在、attempt `ended_at` 非空）、`unit.test_codex_executor.test_timeout_marks_timed_out` | passed |
| AC10 cancel/retry/approve/reject 改状态，daemon 不覆盖人工结果 | CLI 集成 | `test_cancel_done_task_returns_4`、**新增** `test_cancel_ready_task_returns_0_and_records_event`、`test_retry_failed_task_returns_to_ready`、`test_retry_review_task_returns_ready`、**新增** `test_retry_ready_task_returns_4`、`test_approve_review_task_returns_done`、`test_approve_with_note_records_note_in_event_payload`、`test_reject_review_task_returns_ready`、`test_reject_blocked_task_returns_ready_and_records_event` | passed |
| AC10 daemon 尊重 cancel/人工 done | 集成 | `test_ac10_cancel_during_execution_skips_review`、`test_ac10_manual_done_is_not_overwritten`、`test_ac10_cancelled_task_is_not_dispatched`、`test_cancel_terminates_running_subprocess_and_skips_review` | passed |
| 依赖成环、自引用、同批次互引：整批拒绝不落半批 | 单元 + CLI | `unit.test_cards.test_two_card_cycle_is_rejected`、`test_self_reference_is_rejected`、`test_batch_cards_can_reference_each_other`、`test_duplicate_batch_id_is_rejected`；`test_add_invalid_card_returns_2_and_writes_nothing`、`test_add_card_with_path_like_id_returns_2_and_writes_nothing`（断言 `list_tasks()` 为空） | passed |
| `claim_ready` 并发抢占只能有一行成功 | 单元 | **新增** `unit.test_repository.test_claim_ready_concurrent_claim_has_single_winner`（两线程 barrier 同抢，断言一胜一空、attempt_count=1、attempts 仅 1 行） | passed |
| 状态迁移并发（人工命令与 daemon 竞争） | 单元 | `test_concurrent_transition_only_one_wins`、`test_transition_honours_expected_status` | passed |
| 中断（daemon stop）后的 attempt 与 worktree 终态 | 集成 | `test_stop_terminates_registered_subprocess`（本次补断言 worktree 保留）、**新增** `test_stop_when_budget_exhausted_blocks_task`（budget 用尽转 blocked，reason=`interrupted_exhausted`） | passed |
| 取消后的 attempt 与 worktree 终态 | 集成 | `test_cancelled_task_removes_worktree_and_keeps_branch`、`test_cancel_between_worktree_creation_and_transition_removes_worktree`、`test_ac10_cancel_during_execution_skips_review` | passed |
| 并发上限（超出上限的任务本轮不执行） | 集成 | **新增** `test_concurrency_limit_serializes_tasks`（concurrency=1、两任务，峰值并发断言为 1，两者最终 done） | passed |
| worktree 被存活进程占用时不并发写 | 单元 + 集成 | `test_reuse_is_rejected_while_running_marker_pid_is_alive`、`test_live_running_marker_marks_task_worktree_in_use`、`test_worktree_in_use_marks_task_failed`、`test_run_process_records_child_pid_in_marker` | passed |
| 事务中断后不留错位 | 单元 + 集成 | `test_transaction_rolls_back_on_keyboard_interrupt`、`test_interrupt_between_pass_write_and_done_transition_leaves_no_mismatch`、`test_interrupt_between_reject_write_and_ready_transition_leaves_no_mismatch`、`test_interrupt_during_submit_still_stops_and_terminates_children` | passed |

## 测试命令

```bash
cd D:\DevRepository\BitLink

# 改动前基线（现有 112 条）
python3 -m unittest discover -s tests -v

# 改动后全量
python3 -m unittest discover -s tests -v
```

实际输出尾部：改动前 `Ran 112 tests in 117.396s` / `OK`；改动后 `Ran 119 tests in 128.550s` / `OK`。（临时输出文件不在交付范围内，已删除，结论以本报告记录的命令与计数为准。）

## 测试结果

通过 119 条、失败 0、阻断 0。改动前基线 `Ran 112 tests in 117.396s` → `OK`；改动后 `Ran 119 tests in 128.550s` → `OK`，退出码 0。

本次新增 7 条用例，并补强 2 条已有用例的断言：

| 文件 | 新增用例 | 防的回归 |
| --- | --- | --- |
| `tests/unit/test_repository.py` | `test_claim_ready_concurrent_claim_has_single_winner` | 去掉 `BEGIN IMMEDIATE` 或放宽 `WHERE status='ready'` 后同一任务被派发两次（两个进程同改一个 worktree） |
| `tests/integration/test_cli.py` | `test_cancel_ready_task_returns_0_and_records_event` | `CANCELLABLE` 漏掉 ready，导致新建任务无法人工撤销 |
| `tests/integration/test_cli.py` | `test_retry_ready_task_returns_4` | `RETRYABLE` 被放宽到包含 ready，重复 retry 静默成功 |
| `tests/integration/test_cli.py` | `test_daemon_with_held_lock_returns_3` | `cmd_daemon` 不再把 `AlreadyRunning` 映射成退出码 3，单实例约束失效 |
| `tests/integration/test_daemon.py` | `test_stop_when_budget_exhausted_blocks_task` | `_abort_attempt` 忽略 `max_attempts`，连续中断会无限重排队 |
| `tests/integration/test_daemon.py` | `test_concurrency_limit_serializes_tasks` | 线程池 `max_workers` 改为按本轮认领数，并发上限=1 时仍并行跑 |
| `tests/integration/test_dashboard.py` | `test_put_patch_delete_return_405` | 删掉 `do_PATCH`/`do_DELETE` 后返回 501 而非明确的 405 |

补强断言：`test_ac9_timeout_fails_without_review` 增加 worktree 目录、`bitlink/T` 分支、attempt `ended_at` 断言（防止超时被错误接到取消路径而删掉证据）；`test_stop_terminates_registered_subprocess` 增加 worktree 保留断言（防止中断被错误接到取消路径）。

新用例均为对照实现行为编写：例如 `test_daemon_with_held_lock_returns_3` 在测试进程持有 `DaemonLock` 时启动真实 `bitlink daemon` 子进程，断言退出码 3 且 stderr 含「已有」；`test_claim_ready_concurrent_claim_has_single_winner` 用两个独立连接 + `threading.Barrier` 同时调用，断言只有一个返回非空。

未发现需要交回 IMPLEMENT 的生产缺陷：上述路径的实际行为与 `02-design/tech-design.md` 的状态迁移表一致。审查报告遗留的 4 条 P2 按指示未修；其中新 P2-1（存活标记一律判 failed）的现状被 `test_live_running_marker_marks_task_worktree_in_use` 固化，日后若按审查建议改成 blocked/等待，需要同步修改该用例。

## 未运行项与剩余风险

- 真实 `codex exec` 端到端 not-run：需要登录凭据、网络与费用，且模型输出不稳定，不适合当回归用例。影响：`--output-schema` 在 Windows 带空格路径下的行为、codex 退出码语义、模型是否按 schema 写报告，只有假执行器与 `codex exec --help` 层面的证据；报告缺失、schema 不合法、evidence 为空三条失败路径已用假执行器覆盖，失败时判 `failed` 并交人工，不会污染 review。
- 真实超时终止 not-run：`taskkill /F /T /PID` 对真实 codex 派生进程树的效果未实测。自动化只验证到假执行器返回 `timed_out=True` 与真实 Python 子进程被终止后 daemon 快速返回。
- 真实 Ctrl+C 信号送达 not-run：Windows 上主线程阻塞在 `future.result()` 时信号是否即时送达未实测；自动化用 `Daemon.stop()` 与伪造线程池覆盖同一条「置停止标志 + terminate_all」路径。
- AC8 的浏览器真实刷新 not-run：验证停在 HTTP 响应含 `content="5"` 与 5 秒 meta refresh 的渲染层，未开浏览器观察 10 秒内的状态变化。
- 不同用户/权限导致只读连接打开失败：只有 mock 覆盖 500 路径，未用真实权限变化实测。
- 审查报告新 P2-2（占位 pid 到子进程 pid 的毫秒级覆盖窗口）未构造用例，按代码路径推得。
- `_fail` 的两段写入（审查报告新 P2-3）未被用例固化，代码中断后可能出现「attempt 已 failed 但任务又 ready」的错位；本次未新增用例，因为修复该类问题属于改实现，超出测试阶段边界。
