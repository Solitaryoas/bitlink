<!-- devflow-template: workflow-summary@1.0 -->
# DevFlow 交付总结

## 交付结果

BitLink 已实现并通过审查、测试与知识沉淀三个门禁。当前状态：可运行的本地任务编排器，零第三方依赖，`python3 -m bitlink` 直接使用。

用户可获得的能力：

- `bitlink init / add / list / show / cancel / retry / approve / reject / daemon / dashboard` 十个命令。
- 多任务按 `blocked_by` 依赖和并发上限（默认 2）派发；每个任务在项目外的独立 git worktree 和 `bitlink/<id>` 分支里执行。
- 执行器调用 `codex exec`，用 JSON Schema 强制结构化报告；`evidence` 为空直接判 failed，不进 review。
- review 是独立进程、只读沙箱、独立会话，对照验收标准和预生成的 `diff.patch` 判定 pass/reject；打回到上限 3 次转 blocked。
- 状态落 SQLite，daemon 崩溃后按「重新入队但受尝试上限约束」恢复；单实例文件锁。
- 只读看板 `127.0.0.1:8787`，5 秒自动刷新，连接层 `mode=ro` + `PRAGMA query_only`。

## 产物

- 需求：`artifacts/bitlink/01-requirement/requirement-report.md`
- 设计：`artifacts/bitlink/02-design/tech-design.md`、`artifacts/bitlink/02-design/execution-plan.md`
- 实现与接口文档：`artifacts/bitlink/03-code/change-report.md`、`artifacts/bitlink/03-code/api-docs.md`
- 审查：`artifacts/bitlink/03-code/review-report.md`（第三轮，P0=0、P1=0、P2=4）
- 测试：`artifacts/bitlink/04-test/test-report.md`
- 知识沉淀：`artifacts/bitlink/05-knowledge/knowledge-report.md`（K1–K7）
- 代码：`bitlink/`（13 个模块 + `schemas/report.schema.json`、`schemas/review.schema.json`）、`bitlink.cmd`
- 测试：`tests/unit`、`tests/integration`、`tests/helpers`

## 验证

- 单元与集成测试：`python3 -m unittest discover -s tests -v` → `Ran 119 tests in 126.162s` / `OK`（协调者在最终轮独立复跑，非引用阶段自述）。
- CLI：`python3 -m bitlink --help` 退出码 0，10 个子命令齐备；`approve --help` 含 `--note`。
- 审查过程：三轮。第一轮 P1=2（review 阶段无失败收敛、退出/取消不终止子进程），第二轮 P1=2（worktree 占用检测失效、看板只读连接实际可写），第三轮 P0/P1 归零。前两轮的 P1 均由审查方用可执行探针证伪实现者的「已修复」声明。
- 关键探针（第三轮审查方实测）：只读连接 `INSERT` 报 `attempt to write a readonly database`，`PRAGMA query_only=1`；worktree 标记内为子进程 pid（46476）而非 daemon pid（5428），存活时抛 `worktree_in_use`，杀掉后可复用且标记清理。
- 状态机校验：`workflow_state.py validate` 通过。
- 未运行（not-run，均因费用或需真实环境）：真实 `codex exec` 端到端、真实 Ctrl+C 信号送达、`taskkill /F /T /PID` 对 codex 进程树的实际效果、浏览器中观察 10 秒刷新、不同用户权限下 `mode=ro` 打开失败。

## 剩余风险

非阻断，均有明确位置，留待真实使用后再决定是否修：

1. 恢复时遇到存活标记一律把任务判 failed，只读 review 的孤儿也会消耗尝试预算（`daemon.py` 恢复分支）。
2. daemon 占位 pid 写入子进程 pid 之间约毫秒级窗口，强杀恰好命中时仍可能复用正在写的目录（`running.py` + `procs.py`）。
3. `_fail` 的 attempt 终态与任务状态迁移仍是两段写入，同类错位窗口比 pass/reject 大（`daemon.py`）。
4. `api-docs.md` 第 14 行「`-shm` 缺失导致只读打开失败」的举例与实测不符（干净关闭后 `mode=ro` 可读）。
5. `events` 表已写入但 `show` 与看板都不渲染，人工排查时看不到事件流（`knowledge-report.md` 记录）。
6. `change-report.md` 第三轮记录的测试数是 111，实际为 112（当前 119），属文档计数偏差，不影响实现。

真实链路尚未验证的三项（`codex exec` 端到端、`--output-schema` 在 Windows 带空格路径下的行为、`taskkill` 杀树）是发布前唯一的实质性未知，验证命令已写在 `change-report.md` 的手工冒烟步骤里。

## 交付后补充（2026-11-26）

真实 `codex exec` 端到端已在临时仓库跑通：`running → review → done`，`diff.patch` 非空，执行与 review 的 session id 不同，主工作区保持干净。为此修正了三处假执行器覆盖不到的问题（详见 `03-code/change-report.md` 末尾）：提示禁止 agent 执行 git、报告改为「JSON 提取 + 工作区兜底文件」、Windows 上改为显式选择沙箱模式。

**使用前必做（仅 Windows）**：在 `<数据目录>/config.json` 写入 `{"exec": {"windows_sandbox": "bypass"}}`。不写时 daemon 退出码 3 并给出提示；`bypass` 意味着执行 agent 拥有当前用户权限，隔离边界只剩 worktree。改为 `service` 会保留沙箱，但 agent 产出的文件可能因 ACL 无法被读取，diff 与人工合并会失效。

当前测试数：137 条，`OK`（1 条 POSIX 专有用例在 Windows 跳过）。上面「未运行项」里的真实 codex 端到端已转为 passed；真实 Ctrl+C、`taskkill` 杀树、大 diff 内联截断后的 review 质量仍未验证。
