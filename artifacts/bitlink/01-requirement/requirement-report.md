<!-- devflow-template: requirement-report@1.0 -->
# 需求报告

## 目标与背景

长链路开发任务（接需求、拆任务、执行、验收）放在单一对话里做会卡在三处：上下文窗口装不下全程、对话关闭后执行就停、模型自己说「已完成」但没有证据。模型本身没有自我唤醒能力，所以流程的时钟必须来自对话之外。

BitLink 是本地单人使用的任务编排器，把这条链路拆成「聊天框做控制面、daemon 做执行面」：

- 聊天框（现有 Codex CLI 或 Trae 会话）负责接需求、把需求拆成带验收标准的任务卡并写入队列。
- 常驻 daemon 按依赖关系和并发上限派发任务，每个任务在独立 git worktree 里由新的 codex 进程执行。
- 执行完由另一个只读、独立的 review 会话对照验收标准和实际 diff 判定通过或打回。
- 状态落 SQLite，本机只读看板展示进度和证据。

完成后的可观察结果：提交一张任务卡后可以关掉聊天框，daemon 继续推进；打开看板能看到每个任务的状态、尝试次数、最后一次报告和失败原因；连续失败三次的任务停在 blocked 等人处理，而不是无限重试。

## 范围

必须交付：

1. 数据契约：任务卡字段、执行报告字段、任务状态机（ready/running/review/done/failed/blocked/cancelled）。
2. `bitlink` CLI：批量建卡（支持 blocked_by 依赖）、列任务、看详情、取消、重试、人工通过、人工打回。
3. daemon：轮询就绪任务、拓扑依赖判定、并发上限、单任务超时、重试与熔断、优雅退出、重启后不重复执行已完成任务。
4. 执行器：调用 `codex exec`，把任务卡和项目 `AGENTS.md` 路径注入首条提示，用 JSON Schema 强制结构化报告，原始事件存 JSONL。
5. 工作区隔离：每个任务一个 `git worktree` 和独立分支，任务结束按结果保留或清理。
6. review：独立 codex 进程、只读沙箱，输入 acceptance、git diff 和执行报告，输出 pass/reject 及理由。
7. 存储：SQLite 单文件，记录任务、每次尝试、报告、日志路径、时间戳。
8. 只读看板：本机 HTTP 页面，列表加详情，自动刷新。
9. 失败处理：review 打回后回到 ready 并累加尝试次数；达到上限转 blocked 并写明阻塞原因。
10. 日志：每次尝试一份 codex JSONL 原始事件和一份 report.json，路径写进数据库。

边界：

- 单机、单用户、单仓库；一个 daemon 实例管理一个项目根目录。
- 由用户手动启动 daemon，不做 Windows 服务安装。
- 只接受 git 仓库；非 git 目录拒绝建卡。
- Windows 优先，不依赖 Unix 专属命令。

## 排除项

- 自建 Web 聊天界面、模型接入、流式对话协议。
- 多用户、账号体系、权限隔离、公网访问。
- 自动合并到 main、自动 push、自动开 PR。
- 多仓库编排与跨仓库依赖。
- 分布式队列、远程 worker、消息中间件。
- 除 codex 外的执行器实现（只留适配接口，不实现 claude/kimi 适配）。
- 需求自动拆解：拆分由聊天框里的 agent 完成，BitLink 只接受已拆好的任务卡。
- 桌面通知、手机端提醒（首版用看板和日志）。

## 决策与理由

- **本地单人（用户确认）**：账号、权限、部署的成本与「自动跑完长链路」这个核心收益无关，先砍掉。
- **复用现有会话当聊天框（用户确认）**：不做聊天 UI 和模型接入，BitLink 只暴露 CLI，让任意会执行 shell 的 agent 都能建卡。
- **Python 3 标准库优先（用户确认）**：`sqlite3`、`subprocess`、`threading`、`http.server` 覆盖全部需求，零第三方依赖即可分发；具体 Web 方案留给 DESIGN。
- **首个执行器只接 codex**：环境里只有 codex CLI 可用，且它原生支持 `--json`、`--output-schema`、`-o`、`-C`、`--sandbox`，结构化报告不用靠提示词求。
- **每任务独立 worktree**：并发写同一工作区会互相破坏，这是并行执行的前提。
- **review 不自动合并**：合并不可逆，review 通过后停在待人工合并状态。
- **evidence 为空判失败**：报告由执行者自己写，只有磁盘事实（测试输出、diff）能当证据，不允许用 summary 代替。
- **状态落 SQLite 而非对话**：daemon 重启、会话关闭、跨天后仍能重建事实。

## 验收标准

- AC1 建卡：`bitlink add --file cards.json` 写入一张含 acceptance 的卡，退出码 0，数据库 status=ready，看板可见。
- AC2 依赖：A 的 blocked_by 指向 B，B 未完成时 daemon 不派发 A；B 完成后下一轮派发 A。
- AC3 隔离：任务执行期间存在独立 worktree 目录与分支，主工作区 `git status` 无任务产生的改动。
- AC4 结构化报告：完成后 report.json 含 status、summary、evidence、files_changed、blockers 五个字段；evidence 为空时判 failed，不进入 review。
- AC5 review 独立：review 是与执行不同的进程，沙箱只读，输出包含 verdict 与理由，并写入数据库。
- AC6 打回回环：review 打回后任务回到 ready，attempts 加 1，打回理由可查；第 3 次失败后 status=blocked。
- AC7 崩溃恢复：daemon 在任务 running 时被强制结束，重启后同一任务不会出现两个并行执行。
- AC8 看板：浏览器能看到任务列表、状态、attempts、最后一次报告摘要，10 秒内反映状态变化。
- AC9 超时：任务超过配置超时时间被终止，记录 timeout，不进入 review。
- AC10 人工干预：cancel、retry、approve、reject 四个命令能改变状态，且 daemon 尊重人工结果不再覆盖。

## 假设与证据

- `codex-cli 0.163.0-alpha.11.2` 已安装，`codex exec` 支持 `--json`、`--output-schema`、`-o/--output-last-message`、`-C`、`--sandbox`、`resume`、`fork`、`review`（本机命令输出，2026-11-25）。
- 本机 Python 3、git 2.56.1、Node 24.15.0 可用；claude/kimi/grok CLI 不存在。
- 采用默认值，用户可随时否决：并发上限 2、单任务超时 30 分钟、最大尝试 3 次、看板只读、不自动合并、不引入桌面通知。
- 推断：git 2.56 支持 Windows 下的 `git worktree`；未在本项目实际验证，DESIGN 前会跑一次确认。
- 假设聊天框 agent 能执行 `bitlink` 命令（Codex CLI 具备 shell 能力）。

## 未决问题

阻断项：无。

非阻断：

- 看板用标准库 `http.server` 还是 FastAPI，DESIGN 阶段定。
- 崩溃恢复策略：标记 interrupted 后重新入队一次，还是直接转 blocked 交人，DESIGN 阶段定（倾向重新入队一次）。
- review 是否换模型：当前只有一个 CLI，倾向同模型不同会话，模型名做成配置项。
