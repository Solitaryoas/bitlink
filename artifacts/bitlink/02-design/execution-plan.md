<!-- devflow-template: execution-plan@1.1 -->
# 执行计划

> 保持计划紧凑。只记录执行所需的目标、任务、验证和风险；实际变化与偏差写入 change report，不在这里重复维护过程日志。

## 目标与完成定义

用户拿到一个可直接运行的本地任务编排器：在任意 git 仓库里执行 `python3 -m bitlink init`、`bitlink add --file cards.json` 建卡，再用 `bitlink daemon` 让它自动在独立 worktree 里跑 codex、用只读 review 判定、把状态和证据写进 SQLite，最后用 `bitlink dashboard` 在浏览器看进度。

完成定义：AC1–AC10 全部有对应的自动化测试通过（真实 codex 的端到端改为手工冒烟并记录证据），`python3 -m unittest discover -s tests -v` 全绿，`python3 -m bitlink --help` 与各子命令在当前 Windows 环境可直接执行。

## 上下文与范围

当前 `D:\DevRepository\BitLink` 只有 `artifacts/`，没有代码、`pyproject.toml`、测试目录或 CI，也不是 git 仓库。技术方案见 `artifacts/bitlink/02-design/tech-design.md`。

将新增 `bitlink/` 包（`cli.py`、`config.py`、`db.py`、`cards.py`、`worktree.py`、`review.py`、`daemon.py`、`dashboard.py`、`executors/`、`schemas/`）、根目录 `bitlink.cmd`、`tests/`（unit/integration/helpers）。运行数据写在项目外，默认 `%LOCALAPPDATA%\BitLink\<项目路径哈希前12位>`，包括 `bitlink.db`、`worktrees/`、`logs/`、`daemon.lock`、`config.json`。

已验证的环境事实：git 2.56.1 的 `git worktree` 在 Windows 可用（临时仓库实测通过）；codex-cli 0.163.0-alpha.11.2 的 `codex exec` 支持 `--json`、`--output-schema`、`-o`、`-C`、`-s`；Python 3.14.0 + SQLite 3.50.4；`python` 是 Store 占位（退出码 9009），一律用 `python3`。

非目标：不改目标仓库的被跟踪文件、不自动合并或 push、不自建聊天界面、不做多执行器、不做 Windows 服务、不引入第三方依赖。

## 执行任务

### 任务 T1：搭建包骨架、入口与配置解析

- 状态：`pending`
- 依赖：无
- 目标文件：`bitlink/config.py`
- 具体动作：创建 `bitlink/__init__.py`（版本号）、`bitlink/__main__.py`（转发到 `bitlink.cli.main`）、`bitlink/config.py`、根目录 `bitlink.cmd`。`config.py` 负责：校验项目路径是 git 仓库（`git rev-parse --show-toplevel`）、计算数据目录（默认 `%LOCALAPPDATA%\BitLink\<sha1(项目绝对路径)[:12]>`，支持 `--data-dir`）、读取并合并 `<data-dir>/config.json` 默认值（concurrency=2、poll_interval_seconds=2、timeout_seconds=1800、max_attempts=3、exec.model=null、review.model=null、dashboard.port=8787）、创建数据目录。不写任何业务状态。
- 完成证据：`python3 -c "from bitlink.config import load_config; print(load_config())"` 能打印合并后的配置；在非 git 目录调用返回明确的非 0 退出码与提示；`python3 -m bitlink --help` 显示全部子命令名（此时可先只注册，未实现的方法返回「未实现」）。

### 任务 T2：实现 SQLite 存储层与状态迁移

- 状态：`pending`
- 依赖：T1
- 目标文件：`bitlink/db.py`
- 具体动作：实现 `connect(data_dir)`（`journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout=5000`）、`migrate()`（`meta.schema_version` 从 1 开始，`CREATE TABLE IF NOT EXISTS` 覆盖 `meta`、`tasks`、`attempts`、`events`，字段按技术方案；`tasks(blocked_by)` 用 JSON 文本，`attempts` 对 `(task_id, attempt_no)` 唯一）、`Repository` 类提供 `create_cards`（单事务批量插入）、`transition(task_id, expect, to, **fields)`（`BEGIN IMMEDIATE` + `UPDATE ... WHERE status IN (...)`，返回是否命中）、`claim_ready(limit)`（依赖门控查询 + 条件更新到 running + `attempt_count+1` + 插入 attempt，同一事务）、`record_attempt_phase`、`recover_running`、`list_tasks`、`show_task`。所有值参数用占位符，表名列名写常量。
- 完成证据：单元测试 `tests/unit/test_repository.py` 覆盖建卡、成功迁移、非法迁移被拒、两次并发 `transition` 只有一次返回 True、`claim_ready` 跳过依赖未完成的任务；`meta.schema_version` 存在且等于 1。

### 任务 T3：任务卡解析与依赖校验

- 状态：`pending`
- 依赖：T2
- 目标文件：`bitlink/cards.py`
- 具体动作：解析 `bitlink add --file cards.json` 的输入（顶层数组或 `{"cards": [...]}`，每张卡字段 `title`、`description`、`acceptance`、可选 `blocked_by`、`max_attempts`、`timeout_seconds`），校验必填字段、类型、`acceptance` 非空；校验 `blocked_by` 的 id 必须存在于数据库或同批次内，禁止自引用，用 DFS 检测环；任何一张不合法则整批拒绝（不落库）。生成 `uuid4().hex` 作为 id。
- 完成证据：单元测试 `tests/unit/test_cards.py` 覆盖合法单卡、批量互引、未知依赖、自引用、两卡成环、缺 acceptance，非法输入返回带卡序号的错误信息且数据库无新增行。

### 任务 T4：实现 CLI 子命令

- 状态：`pending`
- 依赖：T2、T3
- 目标文件：`bitlink/cli.py`
- 具体动作：用 `argparse` 注册 `init`、`add`、`list`、`show`、`cancel`、`retry`、`approve`、`reject`、`daemon`、`dashboard`。实现 `init`（建库并写 `meta.project_root`、`meta.base_ref = git rev-parse HEAD`）、`add`（调用 `cards.py`，单事务写入，输出 id 列表）、`list`/`show`（表格与 `--json` 两种输出）、`cancel`/`retry`/`approve`/`reject`（只调用 `Repository.transition`，写 `events` 表，actor=human）。统一退出码：0 成功、2 输入非法、3 不存在或环境不满足、4 状态不允许当前操作。
- 完成证据：`tests/integration/test_cli.py` 用 `subprocess` 跑真实命令，覆盖：`add` 后退出码 0 且 `list --json` 能看到卡片、非法卡退出码 2、不存在的 id 退出码 3、对 done 任务 cancel 退出码 4；`python3 -m bitlink show <id>` 输出含 acceptance 与 attempts。

### 任务 T5：实现 codex 执行器与两个 JSON Schema

- 状态：`pending`
- 依赖：T1
- 目标文件：`bitlink/executors/codex.py`
- 具体动作：定义 `bitlink/executors/base.py` 的 `Executor` 协议（`run(task, worktree_path, attempt_dir, timeout_seconds) -> ExecResult`，结果为退出码、jsonl 路径、report 路径、session id、是否超时）与 `ExecResult` 数据类。实现 `CodexExecutor`：拼装首条提示（任务卡 JSON、项目 `AGENTS.md` 绝对路径并要求先读、worktree 绝对路径、报告字段要求、禁止 push/合并 main），调用 `codex exec -C <worktree> -s workspace-write --json -o <attempt_dir>/report.json --output-schema <package>/schemas/report.schema.json "<prompt>"`，stdout 写 `events.jsonl`、stderr 写 `stderr.log`；从 JSONL 首条事件解析 session id；超时时 `taskkill /F /T /PID`，失败再 `terminate`/`kill`。编写 `schemas/report.schema.json`（status/summary/evidence/files_changed/blockers，evidence 允许空数组）与 `schemas/review.schema.json`（verdict/reason/findings）。
- 完成证据：`tests/unit/test_codex_executor.py` 用假的 `codex` 可执行脚本（临时 `.cmd`/`.py`，通过注入 `executable` 参数替换）验证参数拼装包含 `-C`、`-s workspace-write`、`--output-schema`、`-o`，stdout 落入 `events.jsonl`；用 `json.load` 校验两个 schema 文件可被解析且必填字段齐全；超时分支在 1 秒超时、子进程睡 3 秒时返回 `timed_out=True`。

### 任务 T6：实现 worktree 与分支管理

- 状态：`pending`
- 依赖：T1、T2
- 目标文件：`bitlink/worktree.py`
- 具体动作：`ensure_worktree(project_root, task, base_ref)`：worktree 路径为 `<data-dir>/worktrees/<task_id>`，已存在则复用；否则 `git worktree add -b bitlink/<id> <path> <base_ref>`，失败抛带 stderr 的异常；把路径与分支写回 `tasks`。`remove_worktree(path)`：`git worktree remove --force` 后清理目录。`make_diff(worktree, base_ref)`：生成 `diff.patch` 到 attempt 目录，供 review 使用。
- 完成证据：集成测试 `tests/integration/test_worktree.py` 在临时 git 仓库里建/复用/删除 worktree，断言 `git worktree list` 行数变化、主工作区 `git status --porcelain` 为空（AC3），删除后目录不存在。

### 任务 T7：实现 review 调用与结果解析

- 状态：`pending`
- 依赖：T5、T6
- 目标文件：`bitlink/review.py`
- 具体动作：构造 review 提示（验收标准原文、`diff.patch` 路径、`report.json` 路径、任务标题与描述），调用 `codex exec -C <worktree> -s read-only --json -o <attempt_dir>/review.json --output-schema <package>/schemas/review.schema.json "<prompt>"`，stdout 写 `review.jsonl`，解析 session id 与 verdict/reason/findings，写回 `attempts`。review 进程异常或输出不合法时按 reject 处理并把原始错误写 `reason`，不让任务卡在 review 状态。
- 完成证据：单元测试用注入的假可执行文件，断言命令行含 `-s read-only` 且不含 `--sandbox workspace-write`；`review_session_id` 与 `exec_session_id` 不同（AC5）；review 输出非法 JSON 时返回 reject 且 reason 可读。

### 任务 T8：实现 daemon 调度循环

- 状态：`pending`
- 依赖：T2、T5、T6、T7
- 目标文件：`bitlink/daemon.py`
- 具体动作：启动时对 `<data-dir>/daemon.lock` 加排他锁（Windows `msvcrt.locking`，其他 `fcntl.flock`），拿不到锁退出码 3；执行 `Repository.recover_running`（running → interrupted，按尝试上限回 ready 或 blocked）；主循环按 `poll_interval` 轮询：标记依赖不可恢复的任务为 blocked，`claim_ready(free_slots)`，提交线程池，线程内依次 worktree 准备 → 执行 → 校验报告（缺失/非法/evidence 为空 → failed）→ 生成 diff → review → 按 verdict 迁移状态；每个阶段前重读任务状态，`cancelled` 则终止子进程并跳过后续；捕获 Ctrl+C 后停止领新任务、终止子进程、attempt 标 interrupted、任务按预算回 ready、释放锁。
- 完成证据：`tests/integration/test_daemon.py` 使用 `FakeExecutor`/`FakeReviewer` 覆盖 AC2（B 完成后下一轮派发 A）、AC4（空 evidence 直接 failed 且无 review 调用）、AC6（reject 三次后 blocked，attempt_count=3，理由可查）、AC7（预置 running 行后启动 daemon，恢复为 ready 且只有一个 attempt 在跑；第二个 daemon 进程启动被锁拒绝）、AC9（超时后 failed，reason=timeout）、AC10（运行中 cancel 后 daemon 不再推进，人工 approve 的 done 不被覆盖）。

### 任务 T9：实现只读看板

- 状态：`pending`
- 依赖：T2
- 目标文件：`bitlink/dashboard.py`
- 具体动作：`ThreadingHTTPServer` 绑定 `127.0.0.1:8787`（可用 `--port`/`--host` 覆盖），数据库用 `sqlite3.connect("file:<db>?mode=ro", uri=True)`；实现 `GET /`（列表：id、标题、状态、attempt_count、最近报告摘要、updated_at，含 5 秒 meta refresh）、`GET /tasks/<id>`（任务卡、尝试时间线、报告摘要、review verdict 与理由、日志路径）、`GET /api/tasks`、`GET /api/tasks/<id>`；其他路径 404，非 GET 返回 405；所有数据库文本经 `html.escape`，页面无外部资源。
- 完成证据：`tests/integration/test_dashboard.py` 起真实服务并用 `http.client` 断言 200、列表含任务标题、详情含 review 理由、HTML 头含 `content="5"`、POST 返回 405、标题含 `<script>` 时输出为转义文本（AC8）。

### 任务 T10：补齐单元与集成测试并跑通全量

- 状态：`pending`
- 依赖：T4、T8、T9
- 目标文件：`tests/integration/test_daemon.py`
- 具体动作：创建 `tests/helpers/fake_executor.py`（按脚本写合法报告与 JSONL，可配置睡眠、退出码、报告内容）、`tests/helpers/fake_reviewer.py`（按预设 verdict）、`tests/helpers/temp_repo.py`（临时目录 `git init` + 初始提交）。把 AC1–AC10 逐条映射到测试方法并在方法名里标注（如 `test_ac6_reject_three_times_blocks`），补齐 T2/T3/T5/T7 未覆盖的边界（空 evidence、非法 JSON、依赖成环、并发 transition）。真实 codex 不做自动化，改为手工冒烟步骤写进 `artifacts/bitlink/03-code/change-report.md`。
- 完成证据：`python3 -m unittest discover -s tests -v` 全部通过，输出中能找到每个 `test_ac*` 方法；测试不依赖网络、不调用真实 codex（可用 `Get-Command codex` 不存在时仍通过）。

### 任务 T11：生成看板 HTTP 接口文档

- 状态：`pending`
- 依赖：T9
- 目标文件：`artifacts/bitlink/03-code/api-docs.md`
- 具体动作：按 `C:\Users\Solitaryoas\.trae-cn\skills\devflow\templates\api-docs.md` 生成文档，写入变更概览（新增本机只读看板接口、无外部调用方、不兼容旧版本）、通用约定（仅 GET、绑定 127.0.0.1、无认证、HTML 与 JSON 两种响应、错误为 404/405）、4 个接口的用途、请求、响应字段与示例、错误条件、兼容性说明；响应字段必须与 `dashboard.py` 实际输出一致。
- 完成证据：文件存在且无占位符；文档中的 4 条路径与 `tests/integration/test_dashboard.py` 请求的路径一一对应；`python3 'C:\Users\Solitaryoas\.trae-cn\skills\devflow\scripts\validate_artifacts.py' 'D:\DevRepository\BitLink\artifacts\bitlink' --path 03-code/api-docs.md` 通过。

## 验证与验收

现有测试能力：仓库当前没有任何测试目录、测试框架配置、构建脚本或 CI（根目录仅有 `artifacts/`），因此不存在可复用的 integration、contract、E2E 层级。本次用 Python 标准库 `unittest` 从零建立 unit 与 integration 两层；不引入 pytest、不引入浏览器 E2E 框架。真实 codex 的执行过程不纳入自动化，改为 IMPLEMENT 阶段一次手工冒烟并记录命令与输出。

覆盖映射：

- 单元：T2 状态迁移与并发抢占、T3 依赖校验与环检测、T5 执行器参数拼装与 schema 解析、T7 review 参数与只读沙箱。
- 集成（临时 git 仓库 + 假执行器/假 reviewer + 真 SQLite）：AC1 CLI 建卡、AC2 依赖门控、AC3 worktree 隔离与主工作区干净、AC4 空 evidence 判 failed、AC6 三次打回转 blocked、AC7 崩溃恢复与单实例锁、AC9 超时、AC10 人工命令与 daemon 不覆盖。
- HTTP 契约（真 `ThreadingHTTPServer` + `http.client`）：AC8 列表/详情/5 秒刷新/只读。
- 手工冒烟（不进自动化）：真实 `codex exec` 跑一张只执行 `git status` 的卡，确认 `events.jsonl`、`report.json`、`review.json`、`review.jsonl` 落盘且 review session 与执行 session 不同。

在 `D:\DevRepository\BitLink` 运行：

```bash
# 全量测试（无需网络，不需要真实 codex）
python3 -m unittest discover -s tests -v

# CLI 可运行性与命令面
python3 -m bitlink --help

# worktree 能力冒烟（本机已实测，可重复）
git --version
```

预期观察：`unittest` 输出以 `OK` 结尾，且能看到 `test_ac1_` 到 `test_ac10_` 命名的测试方法；`--help` 列出 10 个子命令且退出码 0；`git --version` 显示 2.56.1.windows.1。任何一条失败都先修实现或测试，不用跳过标记掩盖。

## 风险、恢复与回滚

高风险步骤集中在三处，均可重试且不留脏状态：

1. 真实 codex 调用（T5、T7）。首次冒烟可能因登录、网络、Windows 路径参数失败。恢复方式：失败路径已设计为报告缺失或不合法即判 failed，不会写坏任务；把实际命令行与 stderr 记入 change report，若 `--output-schema` 在 Windows 下不可用，退化为「只依赖 `-o` 输出 + 本地 JSON 校验」，review 同样处理，任务卡与状态机不变。
2. worktree 创建与清理（T6）。失败时任务转 failed、原因 `worktree_setup_failed`，人工修复后用 `bitlink retry`；清理残留用 `git worktree prune`。删除分支用 `git branch -D bitlink/<id>`，不影响主分支。
3. daemon 崩溃与重复启动（T8）。文件锁随进程死亡释放，启动时 `recover_running` 把 running 收敛为 interrupted 再入队，受最大尝试次数约束，最坏情况是任务转 blocked，不会双跑。

回滚整体顺序：停 daemon → 确认无子进程 → 删除数据目录（`%LOCALAPPDATA%\BitLink\<哈希>`）→ `git worktree prune` → 删除 `bitlink/*` 分支 → 移除 `bitlink/`、`bitlink.cmd`、`tests/`。数据库无历史包袱，`schema_version` 仅在 1，迁移失败时删库重建即可；正式数据只有任务与日志，删除前如需留存可整目录打包。
