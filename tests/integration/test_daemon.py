import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from helpers.fake_executor import FakeExecutor
from helpers.fake_reviewer import FakeReviewer
from helpers.temp_repo import git, temp_repo
from bitlink import config as config_module
from bitlink import daemon, db, procs, running, worktree
from bitlink import review as review_module
from bitlink.executors.base import ExecResult


class RegistrySubprocessExecutor:
    """用真实子进程模拟 codex，注册进 daemon 的进程表以验证终止行为。"""

    def __init__(self, registry, sleep_seconds=30):
        self.registry = registry
        self.sleep_seconds = sleep_seconds
        self.calls = []

    def run(self, task, worktree_path, attempt_dir, timeout_seconds):
        os.makedirs(attempt_dir, exist_ok=True)
        self.calls.append(task["id"])
        jsonl_path = os.path.join(attempt_dir, "events.jsonl")
        stderr_path = os.path.join(attempt_dir, "stderr.log")
        command = [sys.executable, "-c", f"import time; time.sleep({self.sleep_seconds})"]
        outcome = procs.run_process(
            command,
            jsonl_path,
            stderr_path,
            timeout_seconds,
            registry=self.registry,
            registry_key=task["id"],
        )
        return ExecResult(
            exit_code=outcome.exit_code,
            jsonl_path=jsonl_path,
            report_path=os.path.join(attempt_dir, "report.json"),
            stderr_path=stderr_path,
            session_id=None,
            timed_out=outcome.timed_out,
        )



def card(task_id, **overrides):
    payload = {
        "id": task_id,
        "title": f"任务 {task_id}",
        "description": "描述",
        "acceptance": "验收通过",
        "blocked_by": [],
        "max_attempts": 3,
        "timeout_seconds": None,
    }
    payload.update(overrides)
    return payload


class _DoneFuture:
    def result(self, timeout=None):
        return None

    def done(self):
        return True


class _InterruptingPool:
    """第二次 submit 抛 KeyboardInterrupt，模拟中断落在提交窗口。"""

    def __init__(self, *args, **kwargs):
        self.submitted = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def submit(self, fn, *args, **kwargs):
        self.submitted += 1
        if self.submitted >= 2:
            raise KeyboardInterrupt
        return _DoneFuture()


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self._repo_ctx = temp_repo()
        self.project = self._repo_ctx.__enter__()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name

        self.config = config_module.load_config(self.project, self.data_dir)
        self.config["poll_interval_seconds"] = 0

        self.repo = db.Repository(db.connect(self.data_dir))
        db.migrate(self.repo.conn)
        self.repo.set_meta("base_ref", config_module.git_head(self.project))

    def tearDown(self):
        self.repo.close()
        self._tmp.cleanup()
        self._repo_ctx.__exit__(None, None, None)

    def make_daemon(self, executor, reviewer=None, max_iterations=1, **config_overrides):
        merged = dict(self.config)
        merged.update(config_overrides)
        return daemon.Daemon(
            merged,
            executor=executor,
            reviewer=reviewer or FakeReviewer(),
            sleep_fn=lambda _seconds: None,
            max_iterations=max_iterations,
        )

    @unittest.skipUnless(os.name == "nt", "Windows 专有的沙箱模式门禁")
    def test_windows_requires_explicit_sandbox_choice(self):
        config = dict(self.config)
        config["exec"] = {"model": None, "windows_sandbox": None}
        instance = daemon.Daemon(config, sleep_fn=lambda _seconds: None, max_iterations=1)
        with self.assertRaises(config_module.ConfigError):
            instance.run()

    def test_dependent_task_starts_from_dependency_commit(self):
        """依赖完成后，下游卡的 worktree 必须包含上游改动，diff 只含自己的改动。

        防的回归：worktree 一律从 base_ref 创建，导致每张卡把前置改动重做一遍，
        review 各自通过但合并时冲突。
        """
        self.repo.create_cards([card("A"), card("B", blocked_by=["A"])])
        observed = {}

        def on_run(task, worktree_path, attempt_dir):
            with open(
                os.path.join(worktree_path, f"{task['id']}.txt"), "w", encoding="utf-8"
            ) as handle:
                handle.write("x\n")
            if task["id"] == "B":
                observed["sees_a"] = os.path.isfile(os.path.join(worktree_path, "A.txt"))

        executor = FakeExecutor(on_run=on_run)
        self.make_daemon(executor, max_iterations=3).run()

        first = self.repo.get_task("A")
        second = self.repo.get_task("B")
        self.assertEqual(first["status"], "done")
        self.assertEqual(second["status"], "done")
        self.assertTrue(first["commit_sha"])
        self.assertEqual(second["base_commit"], first["commit_sha"])
        self.assertTrue(observed["sees_a"])

        diff_path = os.path.join(self.data_dir, "attempts", "B", "1", "diff.patch")
        with open(diff_path, "r", encoding="utf-8", errors="replace") as handle:
            patch = handle.read()
        self.assertIn("B.txt", patch)
        self.assertNotIn("A.txt", patch)

    def test_ac2_dependent_task_waits_for_prerequisite(self):
        self.repo.create_cards([card("A", blocked_by=["B"]), card("B")])
        executor = FakeExecutor()

        self.make_daemon(executor, max_iterations=3).run()

        self.assertEqual(executor.calls, ["B", "A"])
        self.assertEqual(self.repo.get_task("A")["status"], "done")
        self.assertEqual(self.repo.get_task("B")["status"], "done")

    def test_blocked_dependency_marks_task_blocked(self):
        self.repo.create_cards([card("A", blocked_by=["B"]), card("B")])
        self.repo.transition("B", ["ready"], "failed", block_reason="boom")
        executor = FakeExecutor()

        self.make_daemon(executor, max_iterations=1).run()

        task = self.repo.get_task("A")
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["block_reason"], "dependency:B:failed")
        self.assertEqual(executor.calls, [])

    def test_ac4_empty_evidence_fails_without_review(self):
        self.repo.create_cards([card("T")])
        report = {
            "status": "completed",
            "summary": "看起来完成了",
            "evidence": [],
            "files_changed": [],
            "blockers": [],
        }
        executor = FakeExecutor(report=report)
        reviewer = FakeReviewer()

        self.make_daemon(executor, reviewer, max_iterations=1).run()

        task = self.repo.get_task("T")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["block_reason"], "empty_evidence")
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(self.repo.list_attempts("T")[0]["reason"], "empty_evidence")

    def test_ac4_invalid_report_fails(self):
        self.repo.create_cards([card("T")])
        executor = FakeExecutor(invalid_report=True)

        self.make_daemon(executor, max_iterations=1).run()

        task = self.repo.get_task("T")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["block_reason"], "report_invalid")

    def test_ac6_reject_three_times_blocks_task(self):
        self.repo.create_cards([card("T", max_attempts=3)])
        executor = FakeExecutor()
        reviewer = FakeReviewer(verdicts=["reject"], reason="缺少测试证据")

        self.make_daemon(executor, reviewer, max_iterations=6).run()

        task = self.repo.get_task("T")
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["attempt_count"], 3)
        self.assertIn("review_rejected", task["block_reason"])
        self.assertIn("缺少测试证据", task["block_reason"])
        attempts = self.repo.list_attempts("T")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(attempts[-1]["phase"], "rejected")
        self.assertEqual(attempts[-1]["review_reason"], "缺少测试证据")
        self.assertEqual(len(set(a["review_session_id"] for a in attempts)), 1)

    def test_ac7_restart_recovers_running_task_without_double_execution(self):
        self.repo.create_cards([card("T")])
        claimed = self.repo.claim_ready(1)
        self.assertEqual(claimed[0]["id"], "T")
        executor = FakeExecutor()

        self.make_daemon(executor, max_iterations=2).run()

        attempts = self.repo.list_attempts("T")
        self.assertEqual([a["attempt_no"] for a in attempts], [1, 2])
        self.assertEqual(attempts[0]["phase"], "interrupted")
        self.assertEqual(attempts[1]["phase"], "passed")
        self.assertEqual(self.repo.get_task("T")["status"], "done")
        self.assertEqual(executor.calls, ["T"])

    def test_ac7_second_daemon_is_rejected_by_lock(self):
        lock = daemon.DaemonLock(os.path.join(self.data_dir, "daemon.lock"))
        self.assertTrue(lock.acquire())
        try:
            instance = self.make_daemon(FakeExecutor(), max_iterations=1)
            with self.assertRaises(daemon.AlreadyRunning):
                instance.run()
        finally:
            lock.release()

    def test_ac9_timeout_fails_without_review(self):
        self.repo.create_cards([card("T")])
        executor = FakeExecutor(sleep_seconds=2)
        reviewer = FakeReviewer()

        self.make_daemon(executor, reviewer, max_iterations=1, timeout_seconds=1).run()

        task = self.repo.get_task("T")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["block_reason"], "timeout")
        attempt = self.repo.list_attempts("T")[0]
        self.assertEqual(attempt["phase"], "timeout")
        self.assertEqual(attempt["reason"], "timeout")
        self.assertIsNotNone(attempt["ended_at"])
        self.assertEqual(reviewer.calls, [])
        # 超时是「failed」不是「cancelled」：worktree 与分支必须保留，供人工排查。
        # 防的回归：把超时收敛错误地接到取消路径，_handle_cancelled 会把目录删掉。
        self.assertTrue(os.path.isdir(task["worktree_path"]))
        branches = git(self.project, "branch", "--list", "bitlink/T").stdout
        self.assertIn("bitlink/T", branches)

    def test_ac10_cancel_during_execution_skips_review(self):
        self.repo.create_cards([card("T")])
        reviewer = FakeReviewer()
        data_dir = self.data_dir

        def cancel_running(task, worktree_path, attempt_dir):
            other = db.Repository(db.connect(data_dir))
            try:
                other.transition(task["id"], ["running"], "cancelled")
                other.record_event(task["id"], "human", "cancel")
            finally:
                other.close()

        executor = FakeExecutor(on_run=cancel_running)

        self.make_daemon(executor, reviewer, max_iterations=1).run()

        self.assertEqual(self.repo.get_task("T")["status"], "cancelled")
        self.assertEqual(reviewer.calls, [])
        self.assertEqual(self.repo.list_attempts("T")[0]["phase"], "interrupted")

    def test_ac10_manual_done_is_not_overwritten(self):
        self.repo.create_cards([card("T")])
        self.repo.transition("T", ["ready"], "running")
        self.repo.transition("T", ["running"], "review")
        self.repo.transition("T", ["review"], "done")
        executor = FakeExecutor()

        self.make_daemon(executor, max_iterations=2).run()

        self.assertEqual(self.repo.get_task("T")["status"], "done")
        self.assertEqual(executor.calls, [])

    def test_ac10_cancelled_task_is_not_dispatched(self):
        self.repo.create_cards([card("T")])
        self.repo.transition("T", ["ready"], "cancelled")
        executor = FakeExecutor()

        self.make_daemon(executor, max_iterations=2).run()

        self.assertEqual(self.repo.get_task("T")["status"], "cancelled")
        self.assertEqual(executor.calls, [])

    def test_workflow_records_distinct_exec_and_review_sessions(self):
        self.repo.create_cards([card("T")])
        executor = FakeExecutor(session_id="exec-session-9")
        reviewer = FakeReviewer(session_id="review-session-9")

        self.make_daemon(executor, reviewer, max_iterations=1).run()

        attempt = self.repo.list_attempts("T")[0]
        self.assertEqual(attempt["exec_session_id"], "exec-session-9")
        self.assertEqual(attempt["review_session_id"], "review-session-9")
        self.assertNotEqual(attempt["exec_session_id"], attempt["review_session_id"])
        self.assertTrue(os.path.isfile(attempt["exec_jsonl_path"]))
        self.assertTrue(os.path.isfile(attempt["review_jsonl_path"]))

    def test_worktree_is_created_per_task(self):
        self.repo.create_cards([card("T")])
        self.make_daemon(FakeExecutor(), max_iterations=1).run()
        task = self.repo.get_task("T")
        self.assertTrue(task["worktree_path"])
        self.assertTrue(os.path.isdir(task["worktree_path"]))
        self.assertEqual(task["branch"], "bitlink/T")

    def test_ac4_missing_report_file_fails(self):
        self.repo.create_cards([card("T")])
        reviewer = FakeReviewer()
        executor = FakeExecutor(missing_report=True)

        self.make_daemon(executor, reviewer, max_iterations=1).run()

        task = self.repo.get_task("T")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["block_reason"], "report_missing")
        self.assertEqual(reviewer.calls, [])

    def test_dependency_on_missing_task_marks_blocked(self):
        self.repo.create_cards([card("A", blocked_by=["ghost"])])
        executor = FakeExecutor()

        self.make_daemon(executor, max_iterations=1).run()

        task = self.repo.get_task("A")
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["block_reason"], "dependency:ghost:missing")
        self.assertEqual(executor.calls, [])

    def test_review_start_failure_converges_to_reject(self):
        self.repo.create_cards([card("T")])
        broken_reviewer = review_module.CodexReviewer(
            executable=["bitlink-no-such-codex-executable"], project_root=self.project
        )

        self.make_daemon(FakeExecutor(), broken_reviewer, max_iterations=1).run()

        task = self.repo.get_task("T")
        self.assertEqual(task["status"], "ready")
        self.assertIsNone(task["block_reason"])
        attempt = self.repo.list_attempts("T")[0]
        self.assertEqual(attempt["phase"], "rejected")
        self.assertEqual(attempt["review_verdict"], "reject")
        self.assertIn("review_error", attempt["review_reason"])

    def test_restart_recovers_task_stuck_in_review(self):
        self.repo.create_cards([card("T")])
        self.repo.claim_ready(1)
        self.assertTrue(self.repo.transition("T", ["running"], "review"))
        executor = FakeExecutor()

        self.make_daemon(executor, max_iterations=2).run()

        attempts = self.repo.list_attempts("T")
        self.assertEqual([a["attempt_no"] for a in attempts], [1, 2])
        self.assertEqual(attempts[0]["phase"], "interrupted")
        self.assertEqual(attempts[1]["phase"], "passed")
        self.assertEqual(self.repo.get_task("T")["status"], "done")

    def test_fail_converges_from_review_status(self):
        self.repo.create_cards([card("T")])
        self.repo.claim_ready(1)
        self.assertTrue(self.repo.transition("T", ["running"], "review"))
        instance = self.make_daemon(FakeExecutor(), max_iterations=0)
        instance.repo = self.repo

        instance._fail("T", 1, "internal_error", "boom")

        task = self.repo.get_task("T")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["block_reason"], "internal_error")
        self.assertEqual(self.repo.list_attempts("T")[0]["phase"], "failed")

    def test_fail_records_event_when_transition_misses(self):
        self.repo.create_cards([card("T")])
        self.assertTrue(self.repo.transition("T", ["ready"], "done"))
        instance = self.make_daemon(FakeExecutor(), max_iterations=0)
        instance.repo = self.repo

        instance._fail("T", 1, "internal_error", "boom")

        self.assertEqual(self.repo.get_task("T")["status"], "done")
        row = self.repo.conn.execute(
            "SELECT * FROM events WHERE task_id = 'T' AND type = 'failed_transition_missed'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_interrupt_between_pass_write_and_done_transition_leaves_no_mismatch(self):
        self.repo.create_cards([card("T")])
        instance = self.make_daemon(FakeExecutor(), FakeReviewer(), max_iterations=1)
        original = db.Repository.transition

        def interrupt_done(repo, task_id, expect, to, **fields):
            if to == db.STATUS_DONE:
                raise KeyboardInterrupt
            return original(repo, task_id, expect, to, **fields)

        with mock.patch.object(db.Repository, "transition", interrupt_done):
            instance.run()

        attempt = self.repo.list_attempts("T")[0]
        self.assertEqual(attempt["phase"], "interrupted")
        self.assertIsNone(attempt["review_verdict"])
        self.assertEqual(self.repo.get_task("T")["status"], "ready")

    def test_interrupt_between_reject_write_and_ready_transition_leaves_no_mismatch(self):
        self.repo.create_cards([card("T")])
        instance = self.make_daemon(
            FakeExecutor(), FakeReviewer(verdicts=["reject"], reason="需要补充测试"), max_iterations=1
        )
        original = db.Repository.transition

        def interrupt_ready(repo, task_id, expect, to, **fields):
            if to == db.STATUS_READY:
                raise KeyboardInterrupt
            return original(repo, task_id, expect, to, **fields)

        with mock.patch.object(db.Repository, "transition", interrupt_ready):
            instance.run()

        attempt = self.repo.list_attempts("T")[0]
        self.assertEqual(attempt["phase"], "interrupted")
        self.assertIsNone(attempt["review_verdict"])
        self.assertEqual(self.repo.get_task("T")["status"], "ready")

    def test_interrupt_during_submit_still_stops_and_terminates_children(self):
        self.repo.create_cards([card("A"), card("B")])
        instance = self.make_daemon(FakeExecutor(), max_iterations=0)
        instance.repo = self.repo

        with mock.patch.object(daemon, "ThreadPoolExecutor", _InterruptingPool), mock.patch.object(
            instance.registry, "terminate_all"
        ) as terminate_all:
            with self.assertRaises(KeyboardInterrupt):
                instance.tick()

        self.assertTrue(instance._stop)
        self.assertTrue(terminate_all.called)

    def test_cancel_between_worktree_creation_and_transition_removes_worktree(self):
        self.repo.create_cards([card("T")])
        original = worktree.ensure_worktree
        data_dir = self.data_dir

        def ensure_then_cancel(*args, **kwargs):
            path = original(*args, **kwargs)
            other = db.Repository(db.connect(data_dir))
            try:
                other.transition("T", ["running"], "cancelled")
                other.record_event("T", "human", "cancel")
            finally:
                other.close()
            return path

        with mock.patch.object(
            daemon.worktree, "ensure_worktree", side_effect=ensure_then_cancel
        ):
            self.make_daemon(FakeExecutor(), max_iterations=1).run()

        task = self.repo.get_task("T")
        self.assertEqual(task["status"], "cancelled")
        self.assertFalse(os.path.isdir(os.path.join(data_dir, "worktrees", "T")))
        self.assertEqual(self.repo.list_attempts("T")[0]["phase"], "interrupted")
        branches = git(self.project, "branch", "--list", "bitlink/T").stdout
        self.assertIn("bitlink/T", branches)

    def test_run_process_records_child_pid_in_marker(self):
        task_id = "P"
        out_path = os.path.join(self.data_dir, "marker-out.log")
        err_path = os.path.join(self.data_dir, "marker-err.log")
        seen = {}
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                marker = running.read_marker(self.data_dir, task_id)
                if marker is not None:
                    seen["pid"] = marker["pid"]
                    return
                time.sleep(0.01)

        watcher = threading.Thread(target=watch)
        watcher.start()
        try:
            procs.run_process(
                [sys.executable, "-c", "import time; time.sleep(1)"],
                out_path,
                err_path,
                10,
                marker_dir=self.data_dir,
                marker_task_id=task_id,
            )
        finally:
            stop.set()
            watcher.join(timeout=5)

        self.assertIsNotNone(seen.get("pid"))
        self.assertNotEqual(seen["pid"], os.getpid())
        self.assertFalse(os.path.isfile(running.marker_path(self.data_dir, task_id)))

    def test_worktree_in_use_marks_task_failed(self):
        self.repo.create_cards([card("T")])
        os.makedirs(os.path.join(self.data_dir, "worktrees", "T"))
        executor = FakeExecutor()

        self.make_daemon(executor, max_iterations=1).run()

        task = self.repo.get_task("T")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["block_reason"], "worktree_in_use")
        self.assertEqual(executor.calls, [])

    def test_live_running_marker_marks_task_worktree_in_use(self):
        self.repo.create_cards([card("T")])
        worktree.ensure_worktree(
            self.project,
            "T",
            self.repo.get_meta("base_ref"),
            os.path.join(self.data_dir, "worktrees"),
        )
        writer = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            running.write_marker(self.data_dir, "T", pid=writer.pid)
            executor = FakeExecutor()

            self.make_daemon(executor, max_iterations=1).run()
        finally:
            writer.kill()
            writer.wait()

        task = self.repo.get_task("T")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["block_reason"], "worktree_in_use")
        self.assertEqual(executor.calls, [])

    def test_running_marker_exists_during_attempt_and_is_removed_after(self):
        self.repo.create_cards([card("T")])
        seen = {}

        def observe(task, worktree_path, attempt_dir):
            marker = running.marker_path(self.data_dir, task["id"])
            seen["exists"] = os.path.isfile(marker)
            if seen["exists"]:
                with open(marker, "r", encoding="utf-8") as handle:
                    seen["payload"] = json.load(handle)

        self.make_daemon(FakeExecutor(on_run=observe), max_iterations=1).run()

        self.assertTrue(seen.get("exists"), "运行期间应存在 running 标记")
        self.assertEqual(seen["payload"]["task_id"], "T")
        self.assertEqual(seen["payload"]["pid"], os.getpid())
        self.assertIn("started_at", seen["payload"])
        self.assertFalse(os.path.isfile(running.marker_path(self.data_dir, "T")))

    def test_cancel_terminates_running_subprocess_and_skips_review(self):
        self.repo.create_cards([card("T")])
        instance = self.make_daemon(None, max_iterations=1)
        instance.executor = RegistrySubprocessExecutor(instance.registry)
        reviewer = FakeReviewer()
        instance.reviewer = reviewer
        data_dir = self.data_dir

        def cancel_when_running():
            other = db.Repository(db.connect(data_dir))
            try:
                for _ in range(200):
                    if other.get_task("T")["status"] == "running":
                        break
                    time.sleep(0.05)
                other.transition("T", ["running"], "cancelled")
                other.record_event("T", "human", "cancel")
            finally:
                other.close()

        canceller = threading.Thread(target=cancel_when_running)
        canceller.start()
        started = time.monotonic()
        try:
            instance.run()
            elapsed = time.monotonic() - started
        finally:
            canceller.join(timeout=5)

        self.assertLess(elapsed, 15)
        self.assertEqual(self.repo.get_task("T")["status"], "cancelled")
        self.assertEqual(self.repo.list_attempts("T")[0]["phase"], "interrupted")
        self.assertEqual(reviewer.calls, [])

    def test_stop_terminates_registered_subprocess(self):
        self.repo.create_cards([card("T")])
        instance = self.make_daemon(None, max_iterations=1)
        instance.executor = RegistrySubprocessExecutor(instance.registry)
        stopper = threading.Timer(1.0, instance.stop)
        stopper.start()

        started = time.monotonic()
        try:
            instance.run()
            elapsed = time.monotonic() - started
        finally:
            stopper.cancel()
            stopper.join(timeout=5)

        self.assertLess(elapsed, 10)
        task = self.repo.get_task("T")
        self.assertEqual(task["status"], "ready")
        self.assertEqual(self.repo.list_attempts("T")[0]["phase"], "interrupted")
        # 中断不是取消：worktree 必须留着给下一次尝试复用。
        # 防的回归：_abort_attempt 走了 _handle_cancelled 的删除分支。
        self.assertTrue(os.path.isdir(task["worktree_path"]))

    def test_stop_when_budget_exhausted_blocks_task(self):
        """中断路径的预算判定：attempt_count 已达上限时转 blocked 而不是无限回 ready。

        防的回归：`_abort_attempt` 忽略 max_attempts，连续 Ctrl+C 会一直重排队。
        """
        self.repo.create_cards([card("T", max_attempts=1)])
        instance = self.make_daemon(FakeExecutor(sleep_seconds=1), max_iterations=1)
        stopper = threading.Timer(0.3, instance.stop)
        stopper.start()
        try:
            instance.run()
        finally:
            stopper.cancel()
            stopper.join(timeout=5)

        task = self.repo.get_task("T")
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["block_reason"], "interrupted_exhausted")
        self.assertEqual(self.repo.list_attempts("T")[0]["phase"], "interrupted")

    def test_concurrency_limit_serializes_tasks(self):
        """并发上限=1 时两个就绪任务不能同时在跑。

        防的回归：tick 把 max_workers 从 concurrency 改成 len(claimed) 后，
        两个 codex 会同时修改各自的 worktree、抢同一份 git 锁。
        """
        self.repo.create_cards([card("A"), card("B")])
        state = {"running": 0, "peak": 0}
        lock = threading.Lock()

        def observe(task, worktree_path, attempt_dir):
            with lock:
                state["running"] += 1
                state["peak"] = max(state["peak"], state["running"])
            time.sleep(0.05)
            with lock:
                state["running"] -= 1

        self.make_daemon(
            FakeExecutor(on_run=observe), max_iterations=2, concurrency=1
        ).run()

        self.assertEqual(state["peak"], 1)
        self.assertEqual(self.repo.get_task("A")["status"], "done")
        self.assertEqual(self.repo.get_task("B")["status"], "done")

    def test_cancelled_task_removes_worktree_and_keeps_branch(self):
        self.repo.create_cards([card("T")])
        data_dir = self.data_dir

        def cancel_running(task, worktree_path, attempt_dir):
            other = db.Repository(db.connect(data_dir))
            try:
                other.transition(task["id"], ["running"], "cancelled")
                other.record_event(task["id"], "human", "cancel")
            finally:
                other.close()

        executor = FakeExecutor(on_run=cancel_running)

        self.make_daemon(executor, max_iterations=1).run()

        task = self.repo.get_task("T")
        self.assertEqual(task["status"], "cancelled")
        self.assertFalse(os.path.isdir(task["worktree_path"]))
        branches = git(self.project, "branch", "--list", "bitlink/T").stdout
        self.assertIn("bitlink/T", branches)


if __name__ == "__main__":
    unittest.main()
