"""SQLite 存储层、迁移与状态迁移。

所有状态变化走 `Repository.transition` 的条件更新（`WHERE status IN (...)`），
派发走 `Repository.claim_ready`，在同一个 `BEGIN IMMEDIATE` 事务里完成
依赖门控、状态改 running、attempt_count 加一与 attempt 行插入。
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA_VERSION = 1
DB_FILENAME = "bitlink.db"

STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_REVIEW = "review"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
STATUS_CANCELLED = "cancelled"

ALL_STATUSES = (
    STATUS_READY,
    STATUS_RUNNING,
    STATUS_REVIEW,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    acceptance TEXT NOT NULL,
    status TEXT NOT NULL,
    blocked_by TEXT NOT NULL DEFAULT '[]',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    timeout_seconds INTEGER,
    block_reason TEXT,
    branch TEXT,
    worktree_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    phase TEXT NOT NULL,
    reason TEXT,
    exec_exit_code INTEGER,
    exec_session_id TEXT,
    exec_jsonl_path TEXT,
    report_path TEXT,
    report_summary TEXT,
    review_verdict TEXT,
    review_reason TEXT,
    review_session_id TEXT,
    review_jsonl_path TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    UNIQUE(task_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT
);
"""

_TASK_UPDATE_FIELDS = frozenset(
    {"block_reason", "branch", "worktree_path", "max_attempts", "timeout_seconds"}
)

_ATTEMPT_UPDATE_FIELDS = frozenset(
    {
        "phase",
        "reason",
        "exec_exit_code",
        "exec_session_id",
        "exec_jsonl_path",
        "report_path",
        "report_summary",
        "review_verdict",
        "review_reason",
        "review_session_id",
        "review_jsonl_path",
        "ended_at",
    }
)

CLAIM_QUERY = """
SELECT t.* FROM tasks t
WHERE t.status = 'ready'
  AND NOT EXISTS (
    SELECT 1 FROM json_each(t.blocked_by) d
    LEFT JOIN tasks dep ON dep.id = d.value
    WHERE dep.id IS NULL OR dep.status <> 'done'
  )
ORDER BY t.created_at, t.id
LIMIT ?
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db_path(data_dir):
    return os.path.join(data_dir, DB_FILENAME)


def connect(data_dir, readonly=False):
    """打开数据库；写入连接开启 WAL、外键与 busy_timeout，只读连接强制 mode=ro。"""
    path = db_path(data_dir)
    if readonly:
        from urllib.parse import quote

        uri = (
            "file:"
            + quote(os.path.abspath(path).replace("\\", "/"), safe="/:")
            + "?mode=ro"
        )
        conn = sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    conn = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def migrate(conn):
    """幂等迁移，`meta.schema_version` 从 1 开始。"""
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
    elif int(row["value"]) < SCHEMA_VERSION:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
        )


def _row_to_dict(row):
    return dict(row) if row is not None else None


class Repository:
    """任务、尝试与事件的读写入口。"""

    def __init__(self, conn):
        self.conn = conn
        self._lock = threading.RLock()

    def close(self):
        with self._lock:
            self.conn.close()

    @contextmanager
    def transaction(self):
        """显式事务边界；中断（KeyboardInterrupt 等）也必须回滚后原样抛出。"""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    # --- meta -----------------------------------------------------------

    def set_meta(self, key, value):
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key, default=None):
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # --- cards ----------------------------------------------------------

    def create_cards(self, cards):
        """单事务批量插入，返回 id 列表。"""
        ids = []
        timestamp = now_iso()
        with self.transaction():
            for card in cards:
                self.conn.execute(
                    "INSERT INTO tasks(id, title, description, acceptance, status, blocked_by, "
                    "attempt_count, max_attempts, timeout_seconds, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                    (
                        card["id"],
                        card["title"],
                        card["description"],
                        card["acceptance"],
                        STATUS_READY,
                        json.dumps(card.get("blocked_by") or [], ensure_ascii=False),
                        card.get("max_attempts", 3),
                        card.get("timeout_seconds"),
                        timestamp,
                        timestamp,
                    ),
                )
                ids.append(card["id"])
        return ids

    # --- state machine --------------------------------------------------

    def transition(self, task_id, expect, to, **fields):
        """条件更新；命中返回 True。`fields` 只允许白名单列。"""
        expect = list(expect)
        if not expect:
            raise ValueError("expect 不能为空")
        unknown = set(fields) - _TASK_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"不允许更新字段：{sorted(unknown)}")

        assignments = ["status = ?", "updated_at = ?"]
        params = [to, now_iso()]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            params.append(value)
        placeholders = ", ".join("?" for _ in expect)
        sql = (
            f"UPDATE tasks SET {', '.join(assignments)} "
            f"WHERE id = ? AND status IN ({placeholders})"
        )
        params.append(task_id)
        params.extend(expect)
        with self._lock:
            cursor = self.conn.execute(sql, params)
            return cursor.rowcount == 1

    def claim_ready(self, limit, actor="daemon"):
        """在同一事务里抢占至多 limit 个依赖已满足的 ready 任务。"""
        if limit <= 0:
            return []
        claimed = []
        timestamp = now_iso()
        with self.transaction():
            rows = self.conn.execute(CLAIM_QUERY, (limit,)).fetchall()
            for row in rows:
                if len(claimed) >= limit:
                    break
                task_id = row["id"]
                cursor = self.conn.execute(
                    "UPDATE tasks SET status = ?, attempt_count = attempt_count + 1, "
                    "updated_at = ? WHERE id = ? AND status = ?",
                    (STATUS_RUNNING, timestamp, task_id, STATUS_READY),
                )
                if cursor.rowcount != 1:
                    continue
                task = dict(
                    self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                )
                attempt_no = task["attempt_count"]
                self.conn.execute(
                    "INSERT INTO attempts(task_id, attempt_no, phase, started_at) "
                    "VALUES(?, ?, ?, ?)",
                    (task_id, attempt_no, STATUS_RUNNING, timestamp),
                )
                self.conn.execute(
                    "INSERT INTO events(task_id, at, actor, type, payload) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        task_id,
                        timestamp,
                        actor,
                        "claimed",
                        json.dumps({"attempt_no": attempt_no}, ensure_ascii=False),
                    ),
                )
                task["current_attempt_no"] = attempt_no
                claimed.append(task)
        return claimed

    def update_attempt(self, task_id, attempt_no, **fields):
        """更新一次尝试；字段只允许白名单列。"""
        unknown = set(fields) - _ATTEMPT_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"不允许更新尝试字段：{sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        params = list(fields.values())
        params.extend([task_id, attempt_no])
        with self._lock:
            self.conn.execute(
                f"UPDATE attempts SET {assignments} WHERE task_id = ? AND attempt_no = ?",
                params,
            )

    def record_event(self, task_id, actor, event_type, payload=None):
        with self._lock:
            self.conn.execute(
                "INSERT INTO events(task_id, at, actor, type, payload) VALUES(?, ?, ?, ?, ?)",
                (
                    task_id,
                    now_iso(),
                    actor,
                    event_type,
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                ),
            )

    def recover_running(self):
        """启动时收敛遗留 running/review：attempt 标 interrupted，任务按预算回 ready 或 blocked。

        review 也要收敛：daemon 在 review 阶段被强杀时任务会停在 review，
        只查 running 会让它永久占用并发槽。规则与技术方案一致——重新入队，
        但 attempt_count 达到 max_attempts 时转 blocked。
        """
        timestamp = now_iso()
        results = []
        with self.transaction():
            rows = self.conn.execute(
                "SELECT * FROM tasks WHERE status IN (?, ?)",
                (STATUS_RUNNING, STATUS_REVIEW),
            ).fetchall()
            for row in rows:
                task = dict(row)
                attempt = self.conn.execute(
                    "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no DESC LIMIT 1",
                    (task["id"],),
                ).fetchone()
                if attempt is not None and attempt["ended_at"] is None:
                    self.conn.execute(
                        "UPDATE attempts SET phase = 'interrupted', "
                        "reason = COALESCE(reason, 'daemon_restarted'), ended_at = ? WHERE id = ?",
                        (timestamp, attempt["id"]),
                    )
                if task["attempt_count"] < task["max_attempts"]:
                    new_status, reason = STATUS_READY, None
                else:
                    new_status, reason = STATUS_BLOCKED, "interrupted_exhausted"
                self.conn.execute(
                    "UPDATE tasks SET status = ?, block_reason = ?, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    (new_status, reason, timestamp, task["id"], task["status"]),
                )
                self.conn.execute(
                    "INSERT INTO events(task_id, at, actor, type, payload) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        task["id"],
                        timestamp,
                        "daemon",
                        "recovered",
                        json.dumps(
                            {"from": task["status"], "status": new_status},
                            ensure_ascii=False,
                        ),
                    ),
                )
                results.append((task["id"], new_status))
        return results

    # --- queries --------------------------------------------------------

    def get_task(self, task_id):
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_dict(row)

    def list_tasks(self, status=None):
        with self._lock:
            if status:
                rows = self.conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY created_at, id", (status,)
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at, id"
                ).fetchall()
        return [dict(row) for row in rows]

    def list_attempts(self, task_id):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no", (task_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_attempt(self, task_id, attempt_no):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM attempts WHERE task_id = ? AND attempt_no = ?",
                (task_id, attempt_no),
            ).fetchone()
        return _row_to_dict(row)

    def count_by_status(self, statuses):
        statuses = list(statuses)
        placeholders = ", ".join("?" for _ in statuses)
        with self._lock:
            row = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM tasks WHERE status IN ({placeholders})", statuses
            ).fetchone()
        return row["n"]

    def show_task(self, task_id):
        task = self.get_task(task_id)
        if task is None:
            return None
        task["attempts"] = self.list_attempts(task_id)
        return task
