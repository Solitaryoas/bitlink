<!-- devflow-template: knowledge-report@1.0 -->
# 知识沉淀报告

## 事实

本机环境（2026-11-25 实测）：Python 3.14.0 / SQLite 3.50.4 / git 2.56.1.windows.1 / codex-cli 0.163.0-alpha.11.2。`python` 命中 Windows Store 占位程序（退出码 9009），所有命令与文档统一写 `python3`。

测试收敛过程：77 → 98 → 111（change-report 记数）/ 112（审查实跑）→ 119 条，全部标准库 `unittest`，零第三方依赖。111 与 112 的差异是 change-report 少记一条，实跑以 112 为准——报告里的计数不能当证据，以命令输出为准。

三轮审查都在「测试全绿」的提交上开始：前两轮各找出 2 条 P1 + 6 条 P2 被打回，第三轮 0 P1 + 4 条 P2 放行。4 条 P1 全部属于「测试通过但行为错」，不是新功能缺失。这一条是本报告的主要动机（见 K6）。

真实未验证项，后文遇到会单独标注：真实 `codex exec` 端到端、真实 Ctrl+C 信号送达、`taskkill /F /T /PID` 杀净 codex 进程树、不同用户/权限下 `mode=ro` 打开失败、占位 pid 到子进程 pid 之间的毫秒级窗口。

## 决策

- **只读消费者用 `file:...?mode=ro` + `PRAGMA query_only=ON` 打开，打开失败直接 500，不回退读写连接。** 边界：要求数据目录对看板进程可写（WAL 连接会建 `-shm`）；跨用户共享目录、只读介质未验证。
- **worktree 复用前的占用判定看数据目录里的真实写进程 pid 标记，`git status` 只留作「目录不是本仓库 worktree」的二次探测。** 边界：pid 被系统回收后重用的情况未处理。
- **attempt 终态与任务状态迁移进同一 `BEGIN IMMEDIATE` 事务。** 边界：`_fail` 仍是两段写入（审查 P2-3 未修），修法相同。
- **条件更新落空一律写事件**，因为落空是正常竞争结果，不写就没有痕迹。
- **人工命令与 daemon 不直接通信**：CLI 只改数据库，daemon 用 0.5 秒监督线程读库并终止对应子进程。
- **review 只要求「不同进程 + 只读沙箱 + 不同会话」，不强制换模型**，模型名留配置项。

## 经验

### K1 SQLite 的「只读」不是 `connect()` 的形参

**现象**：第一版 `db.connect(data_dir, readonly=True)` 命名只读，审查探针直接 `INSERT` 成功；第二版改成 URI 形式 `file:<path>` 但漏了 mode 参数，仍然可写。看板「只读」全靠调用方自觉。

**根因**：`sqlite3.connect(path)` 没有只读参数，按文件权限以读写方式打开；`file:<path>` 不附带 `?mode=ro` 时同样是读写。函数名、形参名、注释都不产生约束。

**正确做法**：

```python
uri = "file:" + quote(os.path.abspath(path).replace("\\", "/"), safe="/:") + "?mode=ro"
conn = sqlite3.connect(uri, uri=True, timeout=5)
conn.execute("PRAGMA query_only=ON")
```

`mode=ro` 是文件层闸，SQLite 打开时即拒绝写；`query_only` 是连接层闸，防这条连接被后续代码复用去写。两层都要。Windows 路径必须把 `\` 换成 `/` 再 quote，否则 `D:\...` 不会被当成合法 URI。

**错误做法为什么错**：把只读做成形参，任何一处绕开这个函数就直接写库，且没有任何报错信号；把「只读」写进文档和注释同样不产生约束。

**如何验证**：在只读连接上直接执行 `INSERT`/`UPDATE`，断言 `sqlite3.OperationalError`，再用读写连接回查该行不存在。断言「调用了只读函数」不算验证。

WAL 的两个实测结论：daemon 正常关闭数据库后（`-wal`/`-shm` 消失），`mode=ro` 仍能打开并读（`test_list_api_survives_daemon_closing_database`）；保留 `-wal`、删掉 `-shm`、目录可写时，SQLite 会重建 `-shm`，只读照样成功。所以「`-shm` 缺失导致只读打不开」是错的排障指引。真正会失败的是目录不可写——未验证，只有 mock 覆盖 500 路径。

### K2 「目录能不能复用」不能用 `git status` 的返回码回答

**现象**：第二轮在复用 worktree 前跑 `git -C <path> status --porcelain`，返回码非 0 判 `worktree_in_use`。审查判 P1：另一个进程正在写这个目录时，`git status` 照样返回 0，检测等于没有。

**根因**：`git status` 检查的是工作区与索引的一致性，不是「有没有人在写」。并发写文件不产生索引冲突；真正会失败的是 `index.lock` 争用，而这与「codex 正在改文件」不是一回事。返回码 0 的语义是「没发现不一致」，不是「没人占用」。

**正确做法**：用真实写进程的 pid 标记，四条规定。

1. 标记写在数据目录（`<data>/running/<task_id>.json`），不写进 worktree——写进去会进 `git add -A` 的 diff，review 看到噪声。
2. pid 必须是**写进程**：daemon 派发时先写自己的 pid 占位，`Popen` 返回后由子进程 pid 覆盖（`procs.py:129-133`），子进程退出只清自己那条。
3. 复用前判活：Windows `tasklist /FI "PID eq <pid>" /NH`，取不到则 ctypes `OpenProcess`；POSIX `os.kill(pid, 0)`，`PermissionError` 视为存活。
4. 死 pid 或标记损坏 → 清理后复用，不阻断；清理一律带 pid 条件（`clear_marker(..., pid=proc.pid)`），否则会误删后写入者的标记。

**错误做法为什么错**：用 daemon 的 pid 标记，daemon 被强杀后 pid 已死、codex 仍活，判活放行，两个进程同改一个目录；写进 worktree 污染 diff；无条件 `os.remove` 让后写入者的标记消失，等于没标记。

**如何验证**：起真实存活子进程（`Popen([sys.executable, "-c", "import time; time.sleep(60)"])`）写标记，断言复用抛 `worktree_in_use`；杀掉后断言可复用且标记消失；写 `{"pid": 999999}` 断言陈旧标记被清。再断言标记里的 pid ≠ 当前测试进程 pid——只断言「抛异常」分不出用的是子进程 pid 还是父进程 pid。

遗留：占位 pid 到子进程 pid 覆盖之间是毫秒窗口（审查 P2-2，未实测）。

### K3 状态机终态与 attempt 终态必须在同一事务

**现象**：review pass 时先提交 `update_attempt(phase="passed")`，再 `transition(review→done)`。进程在两次写之间被杀，留下「attempt 已 passed、任务回 ready」，重启后再跑一遍。

**根因**：两次写入是两个独立提交，中间不存在任何不变量；`recover_running` 只把 `ended_at IS NULL` 的 attempt 标 interrupted，已经写了终态的那条不会被动，恢复逻辑兜不住。

**正确做法**：

```python
with self.repo.transaction():          # BEGIN IMMEDIATE
    self.repo.update_attempt(..., phase="passed", ended_at=db.now_iso())
    changed = self.repo.transition(task_id, ["review"], "done")
```

`transaction()` 必须捕 `BaseException`，回滚后原样抛出——只捕 `Exception` 会漏掉 `KeyboardInterrupt`，留下半个事务。reject 路径同理（`_apply_rejection` 已改）。

**错误做法为什么错**：「靠恢复逻辑兜底」不成立，恢复方无法判断哪一半先写、该信哪一半；在两次写之间加 try/except 补偿也不成立，中断可能正好落在补偿里。

**如何验证**：mock `Repository.transition`，在 `to == "done"` 时抛 `KeyboardInterrupt`，断言 attempt 为 `interrupted`、`review_verdict` 为 None、任务回 `ready`；reject 路径在 `to == "ready"` 处抛中断，断言一致。

可复用的不变量：一个 attempt 的终态和它对应的任务状态必须同真同假。把这句话当 review 检查项，能直接扫出同类「先写 A 再写 B」的代码。当前 `_fail`（`daemon.py:425-433`）仍违反这条，后遗症是超时任务可能多跑一次。

### K4 条件更新的返回值丢了，失败就静默

**现象**：`transition()` 内部是 `UPDATE ... WHERE status IN (...)`，竞争失败时 `rowcount=0`，早期实现丢弃返回值。人工 cancel 与 daemon 派发撞上时，输的一方无声消失，看板只有结果，看不到竞争。

**根因**：条件更新是「谁先写谁赢」的裁决手段，落空是正常结果而非异常，所以不抛错；没有事件记录就没有痕迹。

**正确做法**：每个 `transition` 的 False 都要有处置。`_fail` 落空写 `failed_transition_missed`（带当前状态）；pass 落空写 `done_transition_missed`；reject 落空写 `reject_transition_missed`；取消窗口落空且当前状态是 `cancelled` 时，带刚建的路径调 `_handle_cancelled`，删目录保留分支。

**错误做法为什么错**：`if changed: ...` 后不写 else，等于把「并发输了」当成「什么都没发生」；落空后重试一次 transition 更糟，会覆盖人工结果——这正是 AC10 要防的。

**如何验证**：先把任务迁到 `done`，再调 `_fail`，断言事件表存在 `failed_transition_missed` 且任务仍是 `done`。静态检查：grep 全部 `transition(`，确认每处返回值要么进 `if not changed`，要么有注释说明为何可忽略。

缺口（观测，未修）：事件表目前没有消费入口——`cli show` 与看板详情都只渲染 attempts，`events` 写了没人看。写了不展示，等于把「不静默」降级成「写进一个没人打开的日志」。

### K5 `shutdown(wait=True)` + Ctrl+C + `CREATE_NEW_PROCESS_GROUP` 叠加会卡满超时

**现象**：Ctrl+C 后 daemon 不退，要等一个完整超时周期（默认 1800 秒）；退出前子进程还在跑。第二轮审查判 P1。

**根因**：三件事叠加。`ThreadPoolExecutor.__exit__` 调 `shutdown(wait=True)`，它只等不杀；工作线程阻塞在 `proc.wait(timeout=1800)`；`CREATE_NEW_PROCESS_GROUP` 让子进程收不到主进程的 Ctrl+C 广播，于是没人终止它。另外，用 `_thread.interrupt_main()` 模拟 Ctrl+C 时，主线程阻塞在 `future.result()` 里中断不会立即生效——模拟用例跑满 30 秒，已删除，按未验证处理。

**正确做法**：

- `ProcessRegistry` 按任务 id 登记 `Popen`，`stop()` 里 `terminate_all()`；
- `submit` 与等待包在**同一个** `except KeyboardInterrupt` 里（中断可能落在第二次 submit），收尾抽成 `_drain_futures`；
- 收尾用有界等待循环，每轮先 `stop()` 再 `future.result(timeout=1)`，不用裸 `shutdown(wait=True)`；
- 终止走 `taskkill /F /T /PID`（整棵树），失败再 `terminate` → `kill`。

**错误做法为什么错**：只在 `finally` 里置停止标志不杀进程，工作线程永远等不到 `proc.wait` 返回；`proc.terminate()` 只杀直接子进程，codex 派生的子进程仍持有 worktree。

**如何验证**：真实子进程睡眠 + 注入中断，断言 `elapsed < 5s`、子进程已退出、registry 为空（实测 0.84 秒，子进程退出码 1）。只断言 `terminate_all` 被调用会被骗过——第二轮正有一个测试栽在这里。

### K6 验证「声称已修」：独立上下文 + 可执行探针

**现象**：第二轮修完 8 条问题、98 条测试全绿，第三轮审查仍找出 2 条 P1。

**根因**：测试由实现方写，断言的是「这条代码路径被走到」，不是「外部可观察行为正确」。典型：`test_interrupt_during_submit...` 只断言 `_stop` 与 `terminate_all` 被调用，即使 `terminate_all` 是空实现也绿。

**正确做法**：

1. 审查方用独立上下文，先读代码再写脚本，不采信实现自述与 change-report 的结论句；
2. 探针直接打外部行为：只读连接真执行 `INSERT`；占用检测用真实存活子进程；中断路径用真实子进程测耗时；
3. 反向验证新用例：确认它在旧实现上会红（第三轮审查逐条列了「旧实现会怎样」，从 `TypeError` 到断言红）；
4. 探针输出固定键值（`QUERY_ONLY 1`、`PERSISTED_PROBE 0`、`ELAPSED_SEC 0.84`）连同代码行号写进报告，脚本本身不进仓库。

**错误做法为什么错**：拿「测试数量增加」当修复证据；拿「代码里出现了 `mode=ro` 字样」当只读证据——要看的是写操作会不会失败。

**如何验证**：每条 P1 的验收结论都附一个可复现探针输出，只引用单测名不算。

### K7 长链路里两道流程闸门的实际效果

权限隔离（TEST 阶段只能写测试）：作用是防止「改实现让测试变绿」。事实是发现 4 条遗留 P2 时，测试阶段只补用例、原样交回。代价是新增用例把现状固化了——`test_live_running_marker_marks_task_worktree_in_use` 断言「存活标记一律判 failed」，日后按审查建议改成 blocked 时这条会先红。可复用结论：固化现状的用例必须在测试报告里显式标记，写明它锁的是什么、改动时要同步改哪里，否则后续修复会被一条「看似正确的红」挡住。

恢复熔断（同阶段失败 2 次停止自动流转）：作用是阻止同一方向反复打转。事实是 REVIEW 连续两轮打回后流程停下，要人写下 `failure_limit_overridden`（理由原文：「REVIEW 连续两次打回后熔断，人工放行第三轮修复」）并把 `retry_count` 清零才继续。第三轮找到的 2 条 P1 都是前两轮修法本身的缺陷，不是新方向——说明「再给一次」只有在带来新信息时才值得。可复用结论：熔断放行必须留书面理由，理由是唯一能证明「这次会不同」的证据；理由空白等于默认重跑同一轮。

## 下次做同类编排系统的检查清单

1. 只读消费者是否真的不可写？在只读连接上执行一次 `INSERT` 断言 `OperationalError`，再回查未落库。
2. 「独占某个目录或资源」的判定是否基于真实持有者身份（pid、句柄），而不是返回码、文件存在性或进程内标志？
3. 每个 attempt 的终态与任务状态是否同事务提交？grep 出所有两段式写入，逐个问中断后会不会自相矛盾。
4. 每个条件更新的 `rowcount=0` 是否都有事件记录，且该事件有消费入口（看板或 `show`）？
5. Ctrl+C / 超时 / 人工 cancel 三条终止路径是否共用「登记子进程 → 整树终止 → 有界等待」同一实现，并用真实子进程验证耗时上限？
6. 任务状态是否只有一条写入路径（人工命令与 daemon 共用条件更新），人工结果不会被后续阶段覆盖？
7. 失败路径是否默认收敛到可人工恢复的确定状态（failed/blocked + 原因），而不是中间态或无限自动重试？
8. 验证「已修复」时，是否至少有一条探针直接观测外部行为，并确认该探针在旧实现上会失败？

## 来源

- 需求：`artifacts/bitlink/01-requirement/requirement-report.md`（AC1–AC10）
- 设计与计划：`artifacts/bitlink/02-design/tech-design.md`、`artifacts/bitlink/02-design/execution-plan.md`
- 变更（三轮）：`artifacts/bitlink/03-code/change-report.md`
- 审查（最终轮，含 5 段探针与 4 条 P2）：`artifacts/bitlink/03-code/review-report.md`
- 测试：`artifacts/bitlink/04-test/test-report.md`
- 代码：`bitlink/db.py:131-151`（只读连接）、`bitlink/db.py:183-194`（transaction）、`bitlink/running.py:24-112`（pid 标记与判活）、`bitlink/worktree.py:32-76`（复用判定）、`bitlink/procs.py:15-149`（registry、终止、标记）、`bitlink/daemon.py:204-243`（tick 与 `_drain_futures`）、`bitlink/daemon.py:379-402`（pass 事务）、`bitlink/daemon.py:419-460`（`_fail` 与 `_apply_rejection`）、`bitlink/dashboard.py:36-41`（只读入口）
- 测试证据：`tests/unit/test_repository.py:86`（并发抢占）、`:206`（事务回滚）、`tests/integration/test_dashboard.py:157-175`（只读写失败）、`tests/integration/test_worktree.py:94-146`（存活/陈旧标记）、`tests/integration/test_daemon.py:372-422`（落空事件与中断错位）、`:465`（子进程 pid 写入标记）
- 流程证据：`artifacts/bitlink/workflow-state.json`（`stage_failed` 两次、`failure_limit_overridden` 一次）
