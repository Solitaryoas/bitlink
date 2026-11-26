import json
import os
import subprocess
import sys
import tempfile
import unittest

from helpers.temp_repo import temp_repo
from bitlink import db

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_cli(args, project_path, data_dir, timeout=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    command = [
        sys.executable,
        "-m",
        "bitlink",
        *args,
        "--project",
        project_path,
        "--data-dir",
        data_dir,
    ]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )


class CliTest(unittest.TestCase):
    def setUp(self):
        self._repo_ctx = temp_repo()
        self.repo_path = self._repo_ctx.__enter__()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        result = run_cli(["init"], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        self._tmp.cleanup()
        self._repo_ctx.__exit__(None, None, None)

    def write_cards(self, payload):
        path = os.path.join(self.data_dir, "cards.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        return path

    def repository(self):
        conn = db.connect(self.data_dir)
        return db.Repository(conn)

    def add_card(self, **overrides):
        card = {"title": "写文档", "description": "补 README", "acceptance": "README 有说明"}
        card.update(overrides)
        path = self.write_cards([card])
        result = run_cli(["add", "--file", path, "--json"], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["ids"][0]

    def test_ac1_add_card_exit_zero_and_list_json_shows_it(self):
        card_id = self.add_card()
        result = run_cli(["list", "--json"], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], card_id)
        self.assertEqual(rows[0]["status"], "ready")
        self.assertEqual(rows[0]["blocked_by"], [])

    def test_add_invalid_card_returns_2_and_writes_nothing(self):
        path = self.write_cards([{"title": "缺验收标准", "description": "d"}])
        result = run_cli(["add", "--file", path], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 2)
        self.assertIn("第 1 张卡", result.stderr)
        repo = self.repository()
        try:
            self.assertEqual(repo.list_tasks(), [])
        finally:
            repo.close()

    def test_show_unknown_id_returns_3(self):
        result = run_cli(["show", "not-exists"], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 3)

    def test_cancel_done_task_returns_4(self):
        card_id = self.add_card()
        repo = self.repository()
        try:
            self.assertTrue(repo.transition(card_id, ["ready"], "done"))
        finally:
            repo.close()
        result = run_cli(["cancel", card_id], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 4)
        repo = self.repository()
        try:
            self.assertEqual(repo.get_task(card_id)["status"], "done")
        finally:
            repo.close()

    def test_show_outputs_acceptance_and_attempts(self):
        card_id = self.add_card()
        result = run_cli(["show", card_id], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("acceptance", result.stdout)
        self.assertIn("README 有说明", result.stdout)
        self.assertIn("attempts", result.stdout)

    def test_commands_before_init_return_3(self):
        with tempfile.TemporaryDirectory() as fresh_dir:
            result = run_cli(["list"], self.repo_path, fresh_dir)
        self.assertEqual(result.returncode, 3)
        self.assertIn("init", result.stderr)

    def test_non_git_project_init_returns_3(self):
        with tempfile.TemporaryDirectory() as plain_dir:
            result = run_cli(["init"], plain_dir, self.data_dir)
        self.assertEqual(result.returncode, 3)

    def test_retry_failed_task_returns_to_ready(self):
        card_id = self.add_card()
        repo = self.repository()
        try:
            self.assertTrue(repo.transition(card_id, ["ready"], "failed", block_reason="boom"))
        finally:
            repo.close()
        result = run_cli(["retry", card_id], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        repo = self.repository()
        try:
            task = repo.get_task(card_id)
            self.assertEqual(task["status"], "ready")
            self.assertIsNone(task["block_reason"])
        finally:
            repo.close()

    def test_approve_review_task_returns_done(self):
        card_id = self.add_card()
        repo = self.repository()
        try:
            repo.transition(card_id, ["ready"], "running")
            repo.transition(card_id, ["running"], "review")
        finally:
            repo.close()
        result = run_cli(["approve", card_id], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        repo = self.repository()
        try:
            self.assertEqual(repo.get_task(card_id)["status"], "done")
        finally:
            repo.close()

    def test_approve_with_note_records_note_in_event_payload(self):
        card_id = self.add_card()
        repo = self.repository()
        try:
            repo.transition(card_id, ["ready"], "running")
            repo.transition(card_id, ["running"], "review")
        finally:
            repo.close()
        result = run_cli(
            ["approve", card_id, "--note", "人工确认可放行"], self.repo_path, self.data_dir
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        repo = self.repository()
        try:
            row = repo.conn.execute(
                "SELECT * FROM events WHERE task_id = ? AND type = 'approve'", (card_id,)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(json.loads(row["payload"]), {"note": "人工确认可放行"})
        finally:
            repo.close()

    def test_reject_review_task_returns_ready(self):
        card_id = self.add_card()
        repo = self.repository()
        try:
            repo.transition(card_id, ["ready"], "running")
            repo.transition(card_id, ["running"], "review")
        finally:
            repo.close()
        result = run_cli(
            ["reject", card_id, "--reason", "review 误判"], self.repo_path, self.data_dir
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        repo = self.repository()
        try:
            self.assertEqual(repo.get_task(card_id)["status"], "ready")
        finally:
            repo.close()

    def test_retry_review_task_returns_ready(self):
        card_id = self.add_card()
        repo = self.repository()
        try:
            repo.transition(card_id, ["ready"], "running")
            repo.transition(card_id, ["running"], "review")
        finally:
            repo.close()
        result = run_cli(["retry", card_id], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        repo = self.repository()
        try:
            self.assertEqual(repo.get_task(card_id)["status"], "ready")
        finally:
            repo.close()

    def test_add_card_with_path_like_id_returns_2_and_writes_nothing(self):
        path = self.write_cards(
            [
                {"title": "正常卡", "description": "d", "acceptance": "a"},
                {"id": "../escape", "title": "非法 id", "description": "d", "acceptance": "a"},
            ]
        )
        result = run_cli(["add", "--file", path], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 2)
        self.assertIn("第 2 张卡", result.stderr)
        repo = self.repository()
        try:
            self.assertEqual(repo.list_tasks(), [])
        finally:
            repo.close()

    def test_reject_blocked_task_returns_ready_and_records_event(self):
        card_id = self.add_card()
        repo = self.repository()
        try:
            repo.transition(card_id, ["ready"], "blocked", block_reason="review_rejected: x")
        finally:
            repo.close()
        result = run_cli(
            ["reject", card_id, "--reason", "人工打回"], self.repo_path, self.data_dir
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        repo = self.repository()
        try:
            self.assertEqual(repo.get_task(card_id)["status"], "ready")
            row = repo.conn.execute(
                "SELECT * FROM events WHERE task_id = ? AND type = 'reject'", (card_id,)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["actor"], "human")
        finally:
            repo.close()

    def test_add_with_dependency_on_existing_card(self):
        first = self.add_card()
        path = self.write_cards(
            [
                {
                    "title": "后续任务",
                    "description": "等第一张完成",
                    "acceptance": "有结果",
                    "blocked_by": [first],
                }
            ]
        )
        result = run_cli(["add", "--file", path], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run_cli(["list", "--json"], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        dependent = next(row for row in rows if row["blocked_by"] == [first])
        self.assertIsInstance(dependent["blocked_by"], list)

        result = run_cli(["show", dependent["id"], "--json"], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        shown = json.loads(result.stdout)
        self.assertEqual(shown["blocked_by"], [first])

    def test_cancel_ready_task_returns_0_and_records_event(self):
        """退出码 0 的 cancel 分支此前只有失败分支（4）被覆盖。

        防的回归：CANCELLABLE 列表漏掉 ready，导致新任务无法人工撤销。
        """
        card_id = self.add_card()
        result = run_cli(["cancel", card_id], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        repo = self.repository()
        try:
            self.assertEqual(repo.get_task(card_id)["status"], "cancelled")
            row = repo.conn.execute(
                "SELECT * FROM events WHERE task_id = ? AND type = 'cancel'", (card_id,)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["actor"], "human")
        finally:
            repo.close()

    def test_retry_ready_task_returns_4(self):
        """退出码 4 的另一个分支：ready 不是可重试状态。

        防的回归：RETRYABLE 被放宽到包含 ready 后，重复 retry 会悄悄成功、
        掩盖调用方的错误。
        """
        card_id = self.add_card()
        result = run_cli(["retry", card_id], self.repo_path, self.data_dir)
        self.assertEqual(result.returncode, 4)
        repo = self.repository()
        try:
            self.assertEqual(repo.get_task(card_id)["status"], "ready")
        finally:
            repo.close()

    def test_daemon_with_held_lock_returns_3(self):
        """第二个 daemon 实例必须退出码 3，而不是进入调度循环。

        防的回归：cmd_daemon 不再把 AlreadyRunning 映射成 EXIT_ENV，或锁文件
        路径变化导致单实例约束失效（同一任务被两个 daemon 双跑）。
        """
        from bitlink import daemon as daemon_module

        # Windows 上 daemon 要求显式选择沙箱模式，先写配置，才能走到锁这一层
        with open(os.path.join(self.data_dir, "config.json"), "w", encoding="utf-8") as handle:
            json.dump({"exec": {"windows_sandbox": "service"}}, handle)

        lock = daemon_module.DaemonLock(os.path.join(self.data_dir, "daemon.lock"))
        self.assertTrue(lock.acquire())
        try:
            result = run_cli(["daemon"], self.repo_path, self.data_dir, timeout=30)
        finally:
            lock.release()
        self.assertEqual(result.returncode, 3)
        self.assertIn("已有", result.stderr)


if __name__ == "__main__":
    unittest.main()
