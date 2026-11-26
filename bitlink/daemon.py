"""daemon：单实例文件锁、崩溃恢复、依赖门控派发、超时终止与优雅退出。

每张任务在独立 worktree 里执行；状态变化一律走 Repository 的条件更新，
人工命令与 daemon 的竞争由 SQL 的 `WHERE status IN (...)` 裁决。
"""

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

from bitlink import config as config_module
from bitlink import db, procs, running, worktree
from bitlink.executors.codex import CodexExecutor
from bitlink.jsonout import find_json_object
from bitlink.report import normalize_report
from bitlink.review import CodexReviewer
BLOCKING_DEP_STATUSES = {db.STATUS_CANCELLED, db.STATUS_FAILED, db.STATUS_BLOCKED}
MONITOR_INTERVAL_SECONDS = 0.5


class AlreadyRunning(RuntimeError):
    """已有 daemon 实例持有文件锁。"""


class DaemonLock:
    """跨平台排他文件锁：Windows 用 msvcrt，POSIX 用 fcntl。"""

    def __init__(self, path):
        self.path = path
        self._handle = None

    def acquire(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self):
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def load_report(path):
    """校验 report.json；返回 (report, error)。error 为 None 表示合法。

    最终消息可能是「说明文字 + JSON」或带 markdown 围栏，也可能先回显任务卡，
    因此扫描所有 JSON 对象，取第一个真正符合报告契约的；校验通过后把
    规范化结果写回同一路径，保证磁盘产物符合契约。
    """
    if not os.path.isfile(path):
        return None, "report_missing"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None, "report_invalid"
    payload = find_json_object(text, lambda item: normalize_report(item) is not None)
    payload = normalize_report(payload) if payload is not None else None
    if payload is None:
        return None, "report_invalid"
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return payload, None


class Daemon:
    def __init__(
        self,
        config,
        executor=None,
        reviewer=None,
        sleep_fn=time.sleep,
        max_iterations=None,
    ):
        self.config = config
        self.project_root = config["project_root"]
        self.data_dir = config["data_dir"]
        self.concurrency = int(config.get("concurrency") or 2)
        self.poll_interval = float(config.get("poll_interval_seconds") or 0)
        self.timeout_seconds = int(config.get("timeout_seconds") or 1800)
        self.worktrees_dir = os.path.join(self.data_dir, "worktrees")
        self.attempts_dir = os.path.join(self.data_dir, "attempts")
        self.sleep_fn = sleep_fn
        self.max_iterations = max_iterations

        self.registry = procs.ProcessRegistry()
        self._active = {}
        self._active_lock = threading.Lock()
        self._monitor_stop = threading.Event()

        exec_config = config.get("exec") or {}
        review_config = config.get("review") or {}
        self.executor = executor or CodexExecutor(
            project_root=self.project_root,
            model=exec_config.get("model"),
            registry=self.registry,
            data_dir=self.data_dir,
            sandbox_mode=exec_config.get("windows_sandbox"),
        )
        self.reviewer = reviewer or CodexReviewer(
            project_root=self.project_root,
            model=review_config.get("model"),
            registry=self.registry,
            data_dir=self.data_dir,
            sandbox_mode=review_config.get("windows_sandbox"),
        )
        self.repo = None
        self.base_ref = None
        self._stop = False

    # --- lifecycle ------------------------------------------------------

    def run(self):
        if os.name == "nt" and isinstance(self.executor, CodexExecutor):
            exec_mode = (self.config.get("exec") or {}).get("windows_sandbox")
            if exec_mode not in ("bypass", "service"):
                raise config_module.ConfigError(
                    "Windows 上必须显式选择 Codex 执行模式。在 "
                    f"{os.path.join(self.data_dir, 'config.json')} 写入 "
                    '{"exec": {"windows_sandbox": "bypass"}}（隔离只靠 worktree，'
                    "agent 拥有当前用户权限）或 \"service\"（保留沙箱，但 agent "
                    "产出的文件可能因 ACL 无法被读取）"
                )
            if exec_mode == "bypass":
                print(
                    "警告：执行 agent 以 --dangerously-bypass-approvals-and-sandbox 运行，"
                    "拥有当前用户的完整权限；隔离边界只有 worktree。",
                    file=sys.stderr,
                )
        lock = DaemonLock(os.path.join(self.data_dir, "daemon.lock"))
        if not lock.acquire():
            raise AlreadyRunning(f"已有 daemon 实例持有锁：{lock.path}")

        self.repo = db.Repository(db.connect(self.data_dir))
        monitor = None
        try:
            db.migrate(self.repo.conn)
            self.base_ref = self.repo.get_meta("base_ref") or config_module.git_head(
                self.project_root
            )
            self.repo.recover_running()
            monitor = threading.Thread(
                target=self._monitor_cancellations,
                name="bitlink-cancel-monitor",
                daemon=True,
            )
            monitor.start()
            iterations = 0
            while not self._stop:
                self.tick()
                iterations += 1
                if self.max_iterations is not None and iterations >= self.max_iterations:
                    break
                self.sleep_fn(self.poll_interval)
        except KeyboardInterrupt:
            self.stop()
            self.repo.recover_running()
        finally:
            self._monitor_stop.set()
            if monitor is not None:
                monitor.join(timeout=2)
            self.repo.close()
            self.repo = None
            lock.release()

    def stop(self):
        """停止调度并终止所有登记在跑的子进程（Ctrl+C 与外部调用共用）。"""
        self._stop = True
        self.registry.terminate_all()

    def _monitor_cancellations(self):
        """轻量监督线程：发现有任务被人工 cancel 时终止它的子进程。

        CLI 只改数据库，不直接与 daemon 通信；daemon 也不能阻塞在
        `executor.run` 里轮询，所以用独立线程按固定间隔读状态。
        """
        while not self._monitor_stop.wait(MONITOR_INTERVAL_SECONDS):
            with self._active_lock:
                active = list(self._active)
            if not active:
                continue
            try:
                tasks = {task["id"]: task for task in self.repo.list_tasks()}
            except Exception:  # noqa: BLE001 - 监督线程不能因一次查询失败退出
                continue
            for task_id in active:
                task = tasks.get(task_id)
                if task is None or task["status"] == db.STATUS_CANCELLED:
                    self.registry.terminate(task_id)

    # --- scheduling -----------------------------------------------------

    def tick(self):
        if self._stop:
            return []
        self._block_unrecoverable_dependencies()
        free_slots = self.concurrency - self.repo.count_by_status(
            [db.STATUS_RUNNING, db.STATUS_REVIEW]
        )
        if free_slots <= 0:
            return []
        claimed = self.repo.claim_ready(free_slots)
        if not claimed:
            return []
        with ThreadPoolExecutor(max_workers=max(1, self.concurrency)) as pool:
            futures = []
            try:
                for task in claimed:
                    futures.append(pool.submit(self._process, task))
                for future in futures:
                    future.result()
            except KeyboardInterrupt:
                # submit 与等待都包在同一个兜底里：中断落在提交窗口时也会先
                # 终止子进程，否则 ThreadPoolExecutor 的 shutdown(wait=True)
                # 会一直等到 codex 自己结束（最长一个超时周期）。
                self._drain_futures(futures)
                raise
        return claimed

    def _drain_futures(self, futures):
        """有界等待本轮 future 结束，每轮先 stop() 兜住「terminate 后才登记」的窗口。"""
        pending = list(futures)
        while pending:
            self.stop()
            for future in pending:
                try:
                    future.result(timeout=1)
                except FuturesTimeoutError:
                    pass
                except Exception:  # noqa: BLE001 - 中断路径只关心尽快收尾
                    pass
            pending = [future for future in pending if not future.done()]

    def _block_unrecoverable_dependencies(self):
        tasks = {task["id"]: task for task in self.repo.list_tasks()}
        for task in tasks.values():
            if task["status"] != db.STATUS_READY:
                continue
            for dependency_id in task["blocked_by"]:
                dependency = tasks.get(dependency_id)
                if dependency is None:
                    reason = f"dependency:{dependency_id}:missing"
                elif dependency["status"] in BLOCKING_DEP_STATUSES:
                    reason = f"dependency:{dependency_id}:{dependency['status']}"
                else:
                    continue
                if self.repo.transition(
                    task["id"], [db.STATUS_READY], db.STATUS_BLOCKED, block_reason=reason
                ):
                    self.repo.record_event(
                        task["id"], "daemon", "blocked", {"reason": reason}
                    )
                break

    # --- one attempt ----------------------------------------------------

    def _process(self, task):
        task_id = task["id"]
        attempt_no = task["current_attempt_no"]
        with self._active_lock:
            self._active[task_id] = attempt_no
        try:
            self._run_attempt(task_id, attempt_no)
        except Exception as exc:  # noqa: BLE001 - 记录后转 failed，避免线程静默死亡
            self._fail(task_id, attempt_no, "internal_error", f"{type(exc).__name__}: {exc}")
        finally:
            with self._active_lock:
                self._active.pop(task_id, None)

    def _run_attempt(self, task_id, attempt_no):
        attempt_dir = os.path.join(self.attempts_dir, task_id, str(attempt_no))

        current = self.repo.get_task(task_id)
        dependencies = (current or {}).get("blocked_by") or []
        try:
            base_commit = worktree.resolve_chain_base(
                self.project_root, self.base_ref, dependencies
            )
        except worktree.WorktreeError as exc:
            self._fail(task_id, attempt_no, exc.reason, str(exc))
            return

        try:
            worktree_path = worktree.ensure_worktree(
                self.project_root,
                task_id,
                base_commit,
                self.worktrees_dir,
                data_dir=self.data_dir,
                on_create=lambda path: self._setup_worktree(path, attempt_dir),
            )
        except worktree.WorktreeError as exc:
            self._fail(task_id, attempt_no, exc.reason, str(exc))
            return

        running.write_marker(self.data_dir, task_id, pid=os.getpid())
        try:
            self._execute_attempt(
                task_id, attempt_no, attempt_dir, worktree_path, base_commit
            )
        finally:
            # 只清自己的标记：exec/review 子进程会把标记改成自己的 pid 并在退出时删除。
            running.clear_marker(self.data_dir, task_id, pid=os.getpid())

    def _setup_worktree(self, worktree_path, attempt_dir):
        """新建 worktree 后执行初始化命令（装依赖等）；失败转 setup 失败。"""
        settings = self.config.get("worktree") or {}
        command = settings.get("setup_command")
        if not command:
            return
        os.makedirs(attempt_dir, exist_ok=True)
        log_path = os.path.join(attempt_dir, "setup.log")
        timeout_seconds = settings.get("setup_timeout_seconds") or 900
        with open(log_path, "wb") as handle:
            try:
                completed = subprocess.run(
                    command,
                    shell=True,
                    cwd=worktree_path,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise worktree.WorktreeError(
                    f"worktree 初始化超时（{timeout_seconds} 秒）：{command}，详见 {log_path}",
                    reason="worktree_setup_failed",
                ) from exc
        if completed.returncode != 0:
            raise worktree.WorktreeError(
                f"worktree 初始化命令失败（退出码 {completed.returncode}）：{command}，详见 {log_path}",
                reason="worktree_setup_failed",
            )

    def _execute_attempt(self, task_id, attempt_no, attempt_dir, worktree_path, base_commit):
        changed = self.repo.transition(
            task_id,
            [db.STATUS_RUNNING],
            db.STATUS_RUNNING,
            branch=worktree.branch_name(task_id),
            worktree_path=worktree_path,
            base_commit=base_commit,
        )
        if not changed:
            current = self.repo.get_task(task_id)
            if current is not None and current["status"] == db.STATUS_CANCELLED:
                # 取消落在「worktree 已建」与「路径落库」之间：task 里没有
                # worktree_path，必须带上刚建的路径清理，否则 git worktree
                # 注册项与目录都会残留。
                self._handle_cancelled(task_id, attempt_no, worktree_path=worktree_path)
                return

        if self._aborted(task_id, attempt_no):
            return

        current = self.repo.get_task(task_id)
        timeout_seconds = current["timeout_seconds"] or self.timeout_seconds
        result = self.executor.run(current, worktree_path, attempt_dir, timeout_seconds)
        self.repo.update_attempt(
            task_id,
            attempt_no,
            exec_exit_code=result.exit_code,
            exec_session_id=result.session_id,
            exec_jsonl_path=result.jsonl_path,
            report_path=result.report_path,
        )

        if self._aborted(task_id, attempt_no):
            return
        if result.timed_out:
            self._fail(task_id, attempt_no, "timeout", "execution timed out", phase="timeout")
            return
        if result.exit_code != 0:
            self._fail(task_id, attempt_no, "exec_failed", f"exec_exit_code={result.exit_code}")
            return

        report, error = load_report(result.report_path)
        if error is not None:
            self._fail(task_id, attempt_no, error, result.stderr_path)
            return
        if not report["evidence"]:
            self._fail(task_id, attempt_no, "empty_evidence", "report.evidence 为空")
            return

        summary = str(report.get("summary") or "")[:500]
        self.repo.update_attempt(task_id, attempt_no, report_summary=summary, phase="completed")
        if not self.repo.transition(task_id, [db.STATUS_RUNNING], db.STATUS_REVIEW):
            self._handle_cancelled(task_id, attempt_no)
            return

        if self._aborted(task_id, attempt_no):
            return

        current = self.repo.get_task(task_id)
        diff_path = os.path.join(attempt_dir, "diff.patch")
        diff_base = (current or {}).get("base_commit") or self.base_ref
        try:
            worktree.make_diff(worktree_path, diff_base, diff_path)
            review_result = self.reviewer.review(
                current, worktree_path, attempt_dir, diff_path, result.report_path, timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 - Popen OSError / SQLite 锁 / 文件权限一律收敛成 reject
            self._apply_rejection(
                task_id,
                attempt_no,
                f"review_error: {type(exc).__name__}: {exc}",
            )
            return

        if self._aborted(task_id, attempt_no):
            return

        if review_result.verdict == "pass":
            # 通过即提交到任务分支：依赖它的卡要能从这个提交起算，人工合并也只需
            # 沿依赖顺序 fast-forward。提交失败按 failed 处理，不留半提交状态。
            try:
                commit_sha = worktree.commit_worktree(
                    worktree_path, f"bitlink: {current['title']} ({task_id})"
                )
            except worktree.WorktreeError as exc:
                self._fail(task_id, attempt_no, exc.reason, str(exc))
                return

            # verdict、attempt 终态与任务状态迁移同一事务提交：进程在中间被杀
            # 不会留下「attempt passed 但 task 回 ready 再跑一遍」的错位。
            with self.repo.transaction():
                self.repo.update_attempt(
                    task_id,
                    attempt_no,
                    phase="passed",
                    review_verdict=review_result.verdict,
                    review_reason=review_result.reason,
                    review_session_id=review_result.session_id,
                    review_jsonl_path=review_result.jsonl_path,
                    ended_at=db.now_iso(),
                )
                changed = self.repo.transition(
                    task_id,
                    [db.STATUS_REVIEW],
                    db.STATUS_DONE,
                    block_reason=None,
                    commit_sha=commit_sha,
                )
            if changed:
                self.repo.record_event(
                    task_id, "daemon", "done", {"attempt_no": attempt_no, "commit": commit_sha}
                )
            else:
                self.repo.record_event(
                    task_id, "daemon", "done_transition_missed", {"attempt_no": attempt_no}
                )
            return

        self._apply_rejection(task_id, attempt_no, review_result.reason)

    # --- helpers --------------------------------------------------------

    def _aborted(self, task_id, attempt_no):
        """在每个阶段推进前检查人工取消与优雅退出；命中时收敛并返回 True。"""
        current = self.repo.get_task(task_id)
        if current is None or current["status"] == db.STATUS_CANCELLED:
            self._handle_cancelled(task_id, attempt_no)
            return True
        if self._stop:
            self._abort_attempt(task_id, attempt_no, "daemon_stopped")
            return True
        return False

    def _fail(self, task_id, attempt_no, reason, detail, phase="failed"):
        """运行失败收敛；running 与 review 两个起点都能落到 failed。

        条件更新落空说明状态已被人工或恢复流程改掉，此时记事件，
        不静默丢弃。
        """
        self.repo.update_attempt(
            task_id, attempt_no, phase=phase, reason=reason, ended_at=db.now_iso()
        )
        changed = self.repo.transition(
            task_id,
            [db.STATUS_RUNNING, db.STATUS_REVIEW],
            db.STATUS_FAILED,
            block_reason=reason,
        )
        if not changed:
            current = self.repo.get_task(task_id)
            self.repo.record_event(
                task_id,
                "daemon",
                "failed_transition_missed",
                {
                    "reason": reason,
                    "detail": detail,
                    "status": current["status"] if current else None,
                },
            )
            return
        self.repo.record_event(
            task_id, "daemon", "failed", {"reason": reason, "detail": detail}
        )

    def _apply_rejection(self, task_id, attempt_no, reason):
        """review 打回（含 review 异常兜底）：按尝试预算回 ready 或 blocked。

        attempt 终态与任务状态迁移同一事务提交，避免进程在中间被杀后留下
        「attempt 已 rejected 但 task 回 ready 再跑一遍」的错位。
        """
        cancelled = False
        with self.repo.transaction():
            self.repo.update_attempt(
                task_id,
                attempt_no,
                phase="rejected",
                review_verdict="reject",
                review_reason=reason,
                ended_at=db.now_iso(),
            )
            current = self.repo.get_task(task_id)
            if current is None or current["status"] == db.STATUS_CANCELLED:
                cancelled = True
            elif current["attempt_count"] < current["max_attempts"]:
                if self.repo.transition(
                    task_id, [db.STATUS_REVIEW], db.STATUS_READY, block_reason=None
                ):
                    self.repo.record_event(
                        task_id,
                        "daemon",
                        "rejected",
                        {"attempt_no": attempt_no, "reason": reason},
                    )
                    return
            else:
                block_reason = f"review_rejected: {reason}"
                if self.repo.transition(
                    task_id, [db.STATUS_REVIEW], db.STATUS_BLOCKED, block_reason=block_reason
                ):
                    self.repo.record_event(
                        task_id, "daemon", "blocked", {"reason": block_reason}
                    )
                    return
            if not cancelled:
                self.repo.record_event(
                    task_id, "daemon", "reject_transition_missed", {"reason": reason}
                )
        if cancelled:
            self._handle_cancelled(task_id, attempt_no)

    def _abort_attempt(self, task_id, attempt_no, reason):
        """中断一次尝试（daemon 停止）：attempt 标 interrupted，任务按预算回 ready/blocked。"""
        self.repo.update_attempt(
            task_id, attempt_no, phase="interrupted", reason=reason, ended_at=db.now_iso()
        )
        current = self.repo.get_task(task_id)
        if current is None or current["status"] == db.STATUS_CANCELLED:
            self._handle_cancelled(task_id, attempt_no)
            return
        if current["attempt_count"] < current["max_attempts"]:
            new_status, block_reason = db.STATUS_READY, None
        else:
            new_status, block_reason = db.STATUS_BLOCKED, "interrupted_exhausted"
        changed = self.repo.transition(
            task_id,
            [db.STATUS_RUNNING, db.STATUS_REVIEW],
            new_status,
            block_reason=block_reason,
        )
        self.repo.record_event(
            task_id,
            "daemon",
            "interrupted",
            {"reason": reason, "status": new_status if changed else None},
        )

    def _handle_cancelled(self, task_id, attempt_no, worktree_path=None):
        """人工取消收敛：attempt 标 interrupted，删除 worktree，保留分支。

        `worktree_path` 用于「已建目录但尚未落库」的窄窗口：此时 task 行里
        还是空值，不能只按数据库字段删。
        """
        self.repo.update_attempt(
            task_id, attempt_no, phase="interrupted", reason="cancelled", ended_at=db.now_iso()
        )
        task = self.repo.get_task(task_id)
        removed = False
        path = worktree_path or (task["worktree_path"] if task else None)
        if path:
            try:
                removed = worktree.remove_worktree(self.project_root, path)
            except OSError:
                removed = False
        self.repo.record_event(
            task_id,
            "daemon",
            "interrupted",
            {"reason": "cancelled", "worktree_removed": removed},
        )
