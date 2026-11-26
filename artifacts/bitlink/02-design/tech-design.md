<!-- devflow-template: tech-design@1.1 -->
# 技术方案

## 背景与目标

结论：交付一个零第三方依赖的 Python 3 包 `bitlink`，提供 CLI、常驻 daemon、codex 执行器、git worktree 隔离、独立 review 进程、SQLite 状态存储和本机只读看板；目标项目的所有运行数据放在项目目录之外，保证任务执行期间主工作区 `git status` 干净。

目标行为按需求报告的 AC1–AC10 定义。三条硬约束决定了整体形状：单机单用户单仓库、手动启动 daemon、只接一种执行器（codex）。因此不做队列中间件、不做 Web 后端框架、不做登录鉴权，进程间只通过同一份 SQLite 单文件协作。

`D:\DevRepository\BitLink` 当前只有 `artifacts/` 目录，不是 git 仓库，没有代码、测试、构建配置或 CI。本设计是新建，不需要兼容任何既有实现。

## 证据与约束

需求条目：`artifacts/bitlink/01-requirement/requirement-report.md`（AC1–AC10、排除项、默认值、未决问题）。

本机实测（2026-11-25，本次设计过程中执行）：

- `git version 2.56.1.windows.1`。在 `%TEMP%` 建临时仓库后执行 `git init`、`git commit`、`git worktree add -b task/T1 ...`，`git worktree list` 输出两行（主工作区与 `task/T1` 工作区），退出码 0，随后删除临时目录。结论：Windows 下 `git worktree` 可用，需求报告中的未验证推断成立。
- `codex-cli 0.163.0-alpha.11.2`，`codex exec --help` 确认支持 `--json`、`--output-schema <FILE>`、`-o/--output-last-message`、`-C/--cd`、`-s read-only|workspace-write|danger-full-access`、`--worktree`、`--skip-git-repo-check`，子命令有 `resume`、`fork`、`review`。这些是执行器与 review 直接依赖的接口面。
- Python 3.14.0，`sqlite3.sqlite_version` = 3.50.4，支持 WAL、`BEGIN IMMEDIATE`、`mode=ro` URI 打开。
- 环境里的 `python` 命中 `C:\Users\...\WindowsApps\python.exe` 占位程序，执行即退出码 9009；可用的是 `python3.cmd`（Python Launcher）与 `py -3`。后续所有命令和计划都用 `python3`，避免踩这个坑。

仍未验证（写进风险）：

- 真实 `codex exec` 任务未运行（需要登录凭据与网络，且会产生费用），因此 `--output-schema` 在 Windows 路径参数下的实际行为、codex 退出码语义只能按 CLI 帮助与文档设计，需要 IMPLEMENT 阶段做一次最小冒烟。
- Windows 下 `taskkill /F /T /PID` 能否终止 codex 派生的整棵进程树未实测；设计上把它作为主路径并保留 `terminate→kill` 兜底。

用户确认的约束（来自需求报告）：Python 3 标准库优先、零第三方依赖；不自动合并、不自动 push、不自动开 PR；合并前停在待人工处理状态；evidence 为空判失败；状态必须落 SQLite。

术语（后文复用）：
- 任务卡（card）：一张待执行任务的结构化描述，含标题、描述、验收标准、依赖列表。
- 尝试（attempt）：一次执行记录，从派发到结束；同一个任务可以有多次尝试，每次一份 JSONL 原始事件和一份报告。
- 就绪（ready）：已入队、等依赖满足；就绪不等于可立刻派发，还要看依赖门控。
- 依赖门控：只有当 `blocked_by` 里的任务全部 `done` 时才允许派发。
- 熔断：review 连续打回到尝试上限后停止自动重试，转 `blocked` 等人处理。

## 方案

### 数据模型与状态机

SQLite 单文件，开启 `journal_mode=WAL`（daemon 写、看板只读并发）、`foreign_keys=ON`、`busy_timeout=5000`。`meta` 表存 `schema_version`、`project_root`、`base_ref`。

```sql
meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)

tasks(
  id TEXT PRIMARY KEY,              -- uuid4().hex
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  acceptance TEXT NOT NULL,         -- 原样保存，注入执行与 review 提示
  status TEXT NOT NULL,             -- ready/running/review/done/failed/blocked/cancelled
  blocked_by TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，存任务 id
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  timeout_seconds INTEGER,          -- NULL 用全局默认 1800
  block_reason TEXT,
  branch TEXT,                      -- bitlink/<id>
  worktree_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)

attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  attempt_no INTEGER NOT NULL,
  phase TEXT NOT NULL,              -- running/completed/failed/timeout/interrupted/rejected/passed
  reason TEXT,
  exec_exit_code INTEGER,
  exec_session_id TEXT,             -- codex JSON 事件里的 thread id
  exec_jsonl_path TEXT,
  report_path TEXT,
  report_summary TEXT,
  review_verdict TEXT,              -- pass/reject/NULL
  review_reason TEXT,
  review_session_id TEXT,
  review_jsonl_path TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  UNIQUE(task_id, attempt_no)
)

events(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, at TEXT NOT NULL, actor TEXT NOT NULL, type TEXT NOT NULL, payload TEXT)
```

`attempt_count` 在派发时加 1（不是失败时加）。这样「review 打回后 attempts 加 1」在 AC6 里自然成立：第一次派发后计数为 1，打回再派发变 2，第三次尝试失败时计数已是 3，直接熔断。看板显示的就是「已开始的尝试次数」，语义单一。

状态迁移（全部通过 `Repository.transition(task_id, expect=[...], to=..., **fields)` 完成，SQL 带 `WHERE status IN (...)`，返回 rowcount 是否为 1）：

| 起点 | 事件 | 终点 | 附带动作 |
| --- | --- | --- | --- |
| ready | 依赖全部 done，daemon 派发 | running | 建/复用 worktree 与分支，`attempt_count+1`，插入 attempt |
| running | 报告合法且 evidence 非空 | review | 记录报告路径与摘要 |
| running | 进程非 0 退出、报告缺失/不合法、JSONL 缺失 | failed | `phase` 记原因，`reason` 可查 |
| running | 超时被终止 | failed | `phase=timeout`，不进入 review |
| running | 启动时发现遗留（崩溃） | ready 或 blocked | attempt 标 `interrupted`；`attempt_count < max_attempts` 回 ready，否则 blocked，原因 `interrupted_exhausted` |
| review | verdict=pass | done | 保留 worktree 与分支待人工合并 |
| review | verdict=reject 且 `attempt_count < max_attempts` | ready | 记录 `review_reason`，下次派发复用同一 worktree 与分支 |
| review | verdict=reject 且计数达到上限 | blocked | 原因写 `review_rejected: <最后一次理由>` |
| ready/running/review/failed/blocked | 人工 cancel | cancelled | 终止子进程，`git worktree remove --force`，分支保留 |
| failed/blocked | 人工 retry | ready | 清空 `block_reason`，预算不清零（继续累加，避免无限循环） |
| review | 人工 approve | done | 记录 actor=human 与备注 |
| ready/blocked/failed | 人工 reject | ready | 记录人工理由 |
| ready | 依赖中出现 cancelled/failed/blocked | blocked | 调度扫描时写 `dependency:<id>:<status>` |

「执行失败」与「review 打回」是两条不同路径：review 打回按 AC6 自动回 ready 重试；执行失败（超时、报告不合格、evidence 为空）按 AC4/AC9 的措辞直接判任务 `failed`，交给人工 retry，daemon 不自动重试。差别在于后者通常意味着任务卡本身写得不可执行或环境缺东西，自动重试只会烧 token。备选方案是「执行失败也自动重试到上限」，被否决，理由是该行为会掩盖 AC4 想要暴露的「没有证据」问题，且与 AC9 只要求记录 timeout 的表述不一致。

人工操作与 daemon 的竞争用条件更新解决：daemon 派发时 `WHERE status='ready'`，cancel 时 `WHERE status IN (...)`，两边都只有一个能成功，`rowcount=0` 的一方读到新状态后放弃。AC10 的「daemon 尊重人工结果」由此保证，而不是靠内存标志。

### 依赖与调度

`blocked_by` 在建卡时校验：id 必须存在于库中或同批次卡内，禁止自引用，用 DFS 检测环（含同批次内互相引用），非法则整批拒绝、退出码 2，不落部分数据。

调度每轮做三件事：把依赖已不可恢复（`cancelled`/`failed`/`blocked`）的任务标 `blocked`；用 SQL 找出所有依赖均已 `done` 的 ready 任务，按 `created_at` 升序；可派发数 = 并发上限 − 运行中数量，逐个用条件更新抢占后提交线程池。查询形如：

```sql
SELECT t.* FROM tasks t
WHERE t.status = 'ready'
  AND NOT EXISTS (
    SELECT 1 FROM json_each(t.blocked_by) d
    LEFT JOIN tasks dep ON dep.id = d.value
    WHERE dep.id IS NULL OR dep.status <> 'done'
  )
ORDER BY t.created_at
```

`json_each` 是 SQLite 内置 JSON 函数（3.9+ 起可用，本机 3.50.4 已确认）。这里只做读取判断，不写动态 SQL，参数全部用占位符；表名与列名都是常量，没有拼接用户输入。

并发模型：`ThreadPoolExecutor(max_workers=concurrency)`，每个任务一个线程，线程内用 `subprocess` 跑 codex。默认并发 2、轮询 2 秒、超时 30 分钟、最大尝试 3，全部可被配置文件或 CLI 参数覆盖。

### 进程互斥与崩溃恢复

daemon 启动时对 `<data-dir>/daemon.lock` 取排他锁：Windows 用 `msvcrt.locking(fd, LK_NBLCK, 1)`，其他平台用 `fcntl.flock(LOCK_EX|LOCK_NB)`。拿不到锁立即退出并提示已有实例。选文件锁而不是数据库锁行，是因为进程被杀时操作系统会自动释放文件锁，不会留下需要人肉清理的僵尸锁。

崩溃恢复的结论：**重新入队，但受尝试上限约束**。启动后（拿到锁之后）查询 `status='running'` 的任务，把对应 attempt 标成 `interrupted`、`ended_at` 补齐；`attempt_count < max_attempts` 的任务回 `ready`，否则 `blocked` 并写明 `interrupted_exhausted`。理由：直接转 blocked 会让每次意外断电都需要人工介入；不加约束地重排队会在同一个坏任务上反复烧钱。代价是连续崩溃会消耗重试预算，这一点在看板的 attempts 和 reason 里可见。

优雅退出（Ctrl+C）：停止接受新任务，向所有子进程发终止信号（同超时逻辑），attempt 标 `interrupted`，任务按上面的预算规则回 ready，释放锁后退出。

### 执行器与 worktree

工作区隔离：每个任务一条分支 `bitlink/<task_id>`，从 `meta.base_ref` 指向的提交创建（首次 `init` 时记录 `git rev-parse HEAD`）。worktree 放在项目外：`<data-dir>/worktrees/<task_id>`。数据根目录默认 `%LOCALAPPDATA%\BitLink\<sha1(项目绝对路径)[:12]>`，可用 `--data-dir` 覆盖；项目内不产生任何新路径，`git status` 因此保持干净。

任务结束时的保留策略：

| 结果 | worktree | 分支 |
| --- | --- | --- |
| done | 保留（人工合并要用） | 保留 |
| blocked / failed | 保留（排查证据） | 保留 |
| cancelled | `git worktree remove --force` 后删除目录 | 保留（代码可能有用） |
| 下一次尝试 | 复用同一 worktree 与分支 | 复用 |

`git worktree add` 失败（目录已存在、分支被占用、磁盘权限）时任务直接 `failed`，`reason=worktree_setup_failed`，不消耗 report 流程。

执行器是一个 seam：接口只有 `run(task, worktree_path, attempt_dir, timeout_seconds) -> ExecResult`，返回退出码、JSONL 路径、报告路径和会话 id。当前只有一个 adapter `CodexExecutor`，第二个执行器出现之前不扩接口（一个 adapter 只是假想 seam）。`CodexExecutor` 的实际调用：

```text
codex exec -C <worktree> -s workspace-write --json \
  -o <attempt_dir>/report.json \
  --output-schema <package>/schemas/report.schema.json \
  "<首条提示>"
```

首条提示包含：任务卡 JSON（标题、描述、验收标准）、项目 `AGENTS.md` 的绝对路径（并明确要求先读）、worktree 绝对路径、报告必须写入的结构化要求、禁止提交到 main/禁止 push。stdout 直接重定向到 `<attempt_dir>/events.jsonl`，stderr 到 `stderr.log`，两者路径都写进数据库。

报告 schema（`schemas/report.schema.json`）：`status` 枚举 `completed|failed|blocked`、`summary` 字符串、`evidence` 字符串数组、`files_changed` 字符串数组、`blockers` 字符串数组，`additionalProperties: false`。`evidence` 故意允许空数组：如果 schema 强制至少一条，模型会为了通过校验编造证据。空 evidence 由 daemon 判定并转 `failed`，这是 AC4 的落点。报告解析失败同样判 `failed`。

超时与进程终止：`Popen(..., creationflags=CREATE_NEW_PROCESS_GROUP)`（Windows），超时后先 `taskkill /F /T /PID <pid>`，失败再 `proc.kill()`；POSIX 用 `os.killpg`。结束后无论成败都尝试回收句柄。

会话 id：从 codex JSONL 第一条事件里解析线程 id 并入库，用于证明执行与 review 是两个不同会话（AC5 的可验证证据之一）。

### review

review 是独立子进程，不是线程内调用，也不复用执行会话：

```text
codex exec -C <worktree> -s read-only --json \
  -o <attempt_dir>/review.json \
  --output-schema <package>/schemas/review.schema.json \
  "<review 提示>"
```

提示里给出：验收标准原文、`<attempt_dir>/diff.patch`（daemon 用 `git -C <worktree> diff <base_ref>...HEAD` 预先生成）、`report.json` 路径、任务标题与描述，要求按验收标准逐条判断，找不出证据就 reject。`-s read-only` 保证它不能改代码。review schema：`verdict` 枚举 `pass|reject`、`reason` 字符串、`findings` 数组（每项含 `severity`、`message`）。

review 换不换模型：结论是**不强制换，模型名做成配置**。本机只有 codex 一个 CLI，要求的是「不同进程、只读沙箱、不同会话」，不是「不同模型」；配置项 `exec.model`、`review.model` 默认留空走 CLI 默认，将来有第二个模型时不用改代码。

### CLI

入口为 `python3 -m bitlink`，另提供根目录 `bitlink.cmd` 包装（Windows 优先），不依赖 pip 安装。所有命令共用 `--project`（默认当前目录）、`--data-dir`、`--json`。

| 命令 | 行为 | 退出码 |
| --- | --- | --- |
| `bitlink init` | 校验是 git 仓库，建数据目录与库，记录 `base_ref` | 0 成功 / 3 非 git 目录 |
| `bitlink add --file cards.json` | 批量建卡，整批事务，校验依赖与环 | 0 / 2 校验失败 |
| `bitlink list [--status ready]` | 表格或 JSON 列表 | 0 |
| `bitlink show <id>` | 任务、每次尝试、最近报告摘要、日志路径 | 0 / 3 不存在 |
| `bitlink cancel <id>` | 置 cancelled，通知 daemon 终止 | 0 / 4 状态不允许 |
| `bitlink retry <id>` | failed/blocked → ready | 0 / 4 |
| `bitlink approve <id>` | review → done，写人工备注 | 0 / 4 |
| `bitlink reject <id> --reason` | → ready，写人工理由 | 0 / 4 |
| `bitlink daemon` | 常驻调度 | 0 / 3 已有实例 / 3 非 git 仓库 |
| `bitlink dashboard` | 只读看板 | 0 / 3 端口占用 |

人工命令只改数据库，不直接杀进程；daemon 在每条任务进入下一阶段前重读状态，发现 `cancelled` 就终止子进程并跳过后续阶段。这是「命令与 daemon 解耦」的最低成本做法。

### 看板

结论：用标准库 `http.server.ThreadingHTTPServer`，不引入 FastAPI。理由：需求只有两个只读页面和两个 JSON 读接口，`http.server` 足够；FastAPI 会带来第三方依赖、ASGI 运行时和打包成本，与「零依赖、可分发」冲突。代价是要自己处理线程与转义，这两点在单文件几百行内可控。

看板独立进程运行，用 `sqlite3.connect("file:<db>?mode=ro", uri=True)` 打开数据库，从连接层面保证只读。绑定 `127.0.0.1:8787`（默认），`--host 0.0.0.0` 需显式指定并打印警告。

| 方法 路径 | 用途 |
| --- | --- |
| `GET /` | 任务列表：id、标题、状态、attempts、最近报告摘要、updated_at |
| `GET /tasks/<id>` | 详情：任务卡、尝试时间线、报告摘要、review verdict 与理由、日志路径 |
| `GET /api/tasks` | 列表 JSON |
| `GET /api/tasks/<id>` | 详情 JSON |
| 其他 | 404；非 GET 返回 405 |

自动刷新用 `<meta http-equiv="refresh" content="5">`，满足 AC8 的 10 秒要求，不写前端脚本。所有来自数据库的文本经 `html.escape` 输出；页面无外链资源。写接口一律不存在，避免「只读」变成口头承诺。

### 兼容性与配置

配置文件 `<data-dir>/config.json`，键：`concurrency`(2)、`poll_interval_seconds`(2)、`timeout_seconds`(1800)、`max_attempts`(3)、`exec.model`(null)、`review.model`(null)、`dashboard.port`(8787)。CLI 参数优先于配置文件。库表用 `meta.schema_version` 记版本，打开时按版本顺序执行幂等迁移（`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` 升级），首个版本为 1。

## 影响范围

新增文件（全部新建，不改动任何既有代码）：

- `bitlink/__init__.py`、`bitlink/__main__.py`、`bitlink/cli.py`：入口与参数解析。
- `bitlink/config.py`：路径推导、配置合并、数据目录定位。
- `bitlink/db.py`：连接、WAL、迁移、`Repository`（transition/claim/查询）。
- `bitlink/cards.py`：任务卡解析、依赖校验与环检测。
- `bitlink/worktree.py`：worktree 与分支的创建、复用、清理。
- `bitlink/executors/base.py`、`bitlink/executors/codex.py`：执行器 seam 与 codex 适配。
- `bitlink/review.py`：review 提示构造、调用与结果解析。
- `bitlink/daemon.py`：锁、恢复、调度循环、超时终止、优雅退出。
- `bitlink/dashboard.py`：只读 HTTP 服务与 HTML/JSON 渲染。
- `bitlink/schemas/report.schema.json`、`bitlink/schemas/review.schema.json`。
- `bitlink.cmd`：Windows 启动包装。
- `tests/unit/`、`tests/integration/`、`tests/helpers/`：见验证策略。

数据影响：运行时在项目外生成数据库、日志、worktree；目标仓库内只新增 `bitlink/*` 分支，不修改被跟踪文件。接口影响：新增本机 HTTP 只读接口（见上表），实施阶段必须按模板生成 `artifacts/bitlink/03-code/api-docs.md` 并列入计划（任务 T11）。

兼容性：全新工具，无存量数据迁移；`schema_version` 为将来升级留口。外部依赖：仅 git 与 codex CLI，版本要求写进 `bitlink init` 的启动检查（git ≥ 2.5 才有 worktree，codex 需支持 `--output-schema`）。

## 风险与对策

- 高：真实 codex 行为与设计不符（`--output-schema` 在 Windows 路径失效、退出码语义不同、报告写不出来）。对策：IMPLEMENT 阶段先跑一个最小任务做冒烟并记录实际命令与输出；报告缺失或解析失败按 failed 处理，不会污染 review；路径全部用绝对路径并对空格加引号。
- 高：超时后子进程树残留，继续占用 worktree。对策：`taskkill /T /F` 为主路径，`terminate→kill` 兜底；下一轮派发前检测 worktree 是否仍被占用，占用则判 failed 并保留下日志。
- 中：两个 daemon 同时跑，同一任务双执行。对策：文件排他锁 + 数据库条件更新双保险；锁随进程死亡自动释放，避免僵尸锁。
- 中：Windows 路径长度限制与杀毒软件锁文件导致 `git worktree add/remove` 失败。对策：worktree 放在短路径下（`%LOCALAPPDATA%\BitLink\<12位哈希>`），失败转 failed 并保留原因，不重试。
- 中：任务描述或项目文件里含指令，诱导执行器或 review 越权。对策：review 只读沙箱；执行提示明确禁止 push/合并；残余风险（执行阶段本来就允许写代码）接受并记录。
- 中：日志无限增长（每次尝试一份 JSONL + diff）。对策：报告与事件按尝试目录分片，`bitlink show` 只给路径；首版不做自动清理，在看板显示总占用，留给人工。
- 低：SQLite 并发读写冲突。对策：WAL + `busy_timeout=5000` + 写操作短事务；看板用只读 URI。
- 低：看板 HTML 注入（任务标题或报告含标签）。对策：统一 `html.escape`，页面不引入外部脚本。
- 低：依赖成环或指向不存在任务，导致任务永远不派发。对策：建卡时整批校验（含同批次引用），失败退出码 2，不写半批数据。

## 回滚方案

工具本身不修改目标仓库的被跟踪文件，回滚成本低：

1. 停 daemon，确认无运行中子进程。
2. 删除数据目录（`%LOCALAPPDATA%\BitLink\<哈希>`），数据库、日志、worktree 一并消失；如 worktree 目录残留，执行 `git worktree prune`。
3. 删除 `bitlink/*` 分支：`git branch -D bitlink/<id>`（逐个或 `git branch --list "bitlink/*" | ...`）；已合并的可保留。
4. 移除新增的 `bitlink/` 包、`bitlink.cmd`、`tests/`，仓库回到原状。

数据层面没有生产数据，删库即回滚；`schema_version` 机制保证未来升级时可用「降级脚本 + 备份文件」处理，本次不需要。首版发布前若发现设计缺陷，直接改表结构并把版本重置为 1 是允许的，因为不存在需要兼容的存量库。

## 验证策略

项目当前没有任何测试、构建脚本或 CI（根目录只有 `artifacts/`），所以测试层级从零建立，用标准库 `unittest`，不引入 pytest、不引入浏览器 E2E 框架。分三层：

- 单元测试（`tests/unit/`）：报告与 review 的 JSON 校验、`cards.py` 的依赖校验与环检测、状态迁移的条件更新（并发两次 transition 只有一次成功）、看板 HTML 转义与 meta refresh 周期、配置合并优先级、路径哈希推导。全部纯内存或临时目录，不启动进程。
- 集成测试（`tests/integration/`）：临时目录里 `git init` 建真仓库，注入 `FakeExecutor`（按脚本写合法的 `report.json` 与 `events.jsonl`，不调用 codex）和 `FakeReviewer`（返回预设 pass/reject），跑真实 daemon 调度循环与真实 SQLite，覆盖 AC1、AC2、AC3、AC4、AC6、AC7、AC9、AC10。看板用真 `ThreadingHTTPServer` 起在随机端口，用 `http.client` 请求，覆盖 AC8。
- CLI 契约测试（`tests/integration/test_cli.py`）：`subprocess` 调用 `python3 -m bitlink ...`，断言退出码、stdout JSON 与数据库状态，覆盖 AC1、AC10 的命令面。

真实 codex 的端到端不在自动化范围内：需要登录、网络与费用，且模型输出不稳定，不适合当回归测试。改为在 IMPLEMENT 阶段执行一次手工冒烟（建一张只做 `git status` 的卡，观察 worktree、events.jsonl、report.json、review.json 落盘），命令与观察结果记进 `03-code/change-report.md`。AC5 的「不同进程、只读沙箱」用可验证证据代替模型行为：断言 review 命令行含 `-s read-only`、`review_session_id` 与 `exec_session_id` 不同、两份 JSONL 都存在于不同路径。

高风险项对应的层级：状态机并发（单元 + 集成）、崩溃恢复（集成，构造 running 行后重启 daemon）、超时终止（集成，`FakeExecutor` 睡 2 秒配 1 秒超时）、依赖门控（集成）、看板只读与转义（单元 + 集成）。
