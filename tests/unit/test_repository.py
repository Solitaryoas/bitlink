import json
import tempfile
import threading
import unittest

from bitlink import db


def make_repo(data_dir):
    conn = db.connect(data_dir)
    db.migrate(conn)
    return db.Repository(conn)


def card(task_id, **overrides):
    payload = {
        "id": task_id,
        "title": f"title {task_id}",
        "description": "desc",
        "acceptance": "acc",
        "blocked_by": [],
        "max_attempts": 3,
        "timeout_seconds": None,
    }
    payload.update(overrides)
    return payload


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        self.repo = make_repo(self.data_dir)

    def tearDown(self):
        self.repo.close()
        self._tmp.cleanup()

    def test_schema_version_is_one(self):
        row = self.repo.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["value"], "1")

    def test_create_cards_commits_whole_batch(self):
        ids = self.repo.create_cards([card("t1"), card("t2", blocked_by=["t1"])])
        self.assertEqual(ids, ["t1", "t2"])
        tasks = self.repo.list_tasks()
        self.assertEqual([t["id"] for t in tasks], ["t1", "t2"])
        self.assertEqual(tasks[0]["status"], "ready")
        self.assertEqual(tasks[0]["blocked_by"], [])
        self.assertEqual(tasks[1]["blocked_by"], ["t1"])

    def test_read_boundaries_decode_blocked_by_to_list(self):
        self.repo.create_cards([card("t1"), card("t2", blocked_by=["t1"])])

        tasks = {task["id"]: task for task in self.repo.list_tasks()}
        self.assertEqual(tasks["t1"]["blocked_by"], [])
        self.assertEqual(tasks["t2"]["blocked_by"], ["t1"])
        self.assertIsInstance(tasks["t1"]["blocked_by"], list)
        self.assertIsInstance(tasks["t2"]["blocked_by"], list)

        self.assertEqual(self.repo.get_task("t1")["blocked_by"], [])
        self.assertEqual(self.repo.get_task("t2")["blocked_by"], ["t1"])
        self.assertIsInstance(self.repo.get_task("t2")["blocked_by"], list)
        self.assertEqual(self.repo.show_task("t2")["blocked_by"], ["t1"])

    def test_transition_honours_expected_status(self):
        self.repo.create_cards([card("t1")])
        self.assertTrue(self.repo.transition("t1", ["ready"], "running"))
        self.assertEqual(self.repo.get_task("t1")["status"], "running")
        self.assertFalse(self.repo.transition("t1", ["ready"], "running"))

    def test_transition_rejects_unknown_field(self):
        self.repo.create_cards([card("t1")])
        with self.assertRaises(ValueError):
            self.repo.transition("t1", ["ready"], "running", evil="1 = 1")

    def test_concurrent_transition_only_one_wins(self):
        self.repo.create_cards([card("t1")])
        barrier = threading.Barrier(2)
        results = []

        def worker():
            conn = db.connect(self.data_dir)
            repo = db.Repository(conn)
            try:
                barrier.wait()
                results.append(repo.transition("t1", ["ready"], "running"))
            finally:
                repo.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False, True])

    def test_claim_ready_concurrent_claim_has_single_winner(self):
        """两个连接同时 claim_ready 时只能有一个抢到任务。

        防的回归：把 `BEGIN IMMEDIATE` 去掉、或把 `WHERE status = 'ready'`
        守卫放宽后，同一任务会被派发两次（两个 codex 同改一个 worktree）。
        """
        self.repo.create_cards([card("t1")])
        barrier = threading.Barrier(2)
        claimed = []

        def worker():
            conn = db.connect(self.data_dir)
            repo = db.Repository(conn)
            try:
                barrier.wait()
                claimed.append([task["id"] for task in repo.claim_ready(1)])
            finally:
                repo.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(len(group) for group in claimed), [0, 1])
        self.assertEqual([task_id for group in claimed for task_id in group], ["t1"])
        self.assertEqual(self.repo.get_task("t1")["attempt_count"], 1)
        self.assertEqual(len(self.repo.list_attempts("t1")), 1)

    def test_claim_ready_skips_unmet_dependency(self):
        self.repo.create_cards([card("t1"), card("t2", blocked_by=["t1"])])
        claimed = self.repo.claim_ready(5)
        self.assertEqual([t["id"] for t in claimed], ["t1"])
        self.assertEqual(self.repo.get_task("t2")["status"], "ready")

        self.repo.transition("t1", ["running"], "done")
        claimed = self.repo.claim_ready(5)
        self.assertEqual([t["id"] for t in claimed], ["t2"])

    def test_claim_ready_records_attempt_and_counter(self):
        self.repo.create_cards([card("t1")])
        claimed = self.repo.claim_ready(1)
        self.assertEqual(claimed[0]["current_attempt_no"], 1)
        task = self.repo.get_task("t1")
        self.assertEqual(task["attempt_count"], 1)
        self.assertEqual(task["status"], "running")
        attempts = self.repo.list_attempts("t1")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["attempt_no"], 1)
        self.assertEqual(attempts[0]["phase"], "running")

    def test_claim_ready_respects_limit(self):
        self.repo.create_cards([card("t1"), card("t2"), card("t3")])
        claimed = self.repo.claim_ready(2)
        self.assertEqual(len(claimed), 2)

    def test_update_attempt_writes_whitelisted_fields(self):
        self.repo.create_cards([card("t1")])
        self.repo.claim_ready(1)
        self.repo.update_attempt("t1", 1, phase="failed", reason="timeout", exec_exit_code=1)
        attempt = self.repo.list_attempts("t1")[0]
        self.assertEqual(attempt["phase"], "failed")
        self.assertEqual(attempt["reason"], "timeout")
        self.assertEqual(attempt["exec_exit_code"], 1)
        with self.assertRaises(ValueError):
            self.repo.update_attempt("t1", 1, **{"phase = 'x'": 1})

    def test_recover_running_requeues_within_budget(self):
        self.repo.create_cards([card("t1")])
        self.repo.claim_ready(1)
        result = self.repo.recover_running()
        self.assertEqual(result, [("t1", "ready")])
        self.assertEqual(self.repo.get_task("t1")["status"], "ready")
        self.assertEqual(self.repo.list_attempts("t1")[0]["phase"], "interrupted")

    def test_recover_running_blocks_when_budget_exhausted(self):
        self.repo.create_cards([card("t1", max_attempts=1)])
        self.repo.claim_ready(1)
        result = self.repo.recover_running()
        self.assertEqual(result, [("t1", "blocked")])
        task = self.repo.get_task("t1")
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["block_reason"], "interrupted_exhausted")

    def test_recover_review_task_requeues_within_budget(self):
        self.repo.create_cards([card("t1")])
        self.repo.claim_ready(1)
        self.assertTrue(self.repo.transition("t1", ["running"], "review"))
        result = self.repo.recover_running()
        self.assertEqual(result, [("t1", "ready")])
        task = self.repo.get_task("t1")
        self.assertEqual(task["status"], "ready")
        self.assertIsNone(task["block_reason"])
        self.assertEqual(self.repo.list_attempts("t1")[0]["phase"], "interrupted")

    def test_recover_review_task_blocks_when_budget_exhausted(self):
        self.repo.create_cards([card("t1", max_attempts=1)])
        self.repo.claim_ready(1)
        self.assertTrue(self.repo.transition("t1", ["running"], "review"))
        result = self.repo.recover_running()
        self.assertEqual(result, [("t1", "blocked")])
        task = self.repo.get_task("t1")
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["block_reason"], "interrupted_exhausted")
        self.assertEqual(self.repo.list_attempts("t1")[0]["phase"], "interrupted")

    def test_show_task_returns_none_for_unknown_id(self):
        self.assertIsNone(self.repo.get_task("nope"))

    def test_record_event_appends_row(self):
        self.repo.create_cards([card("t1")])
        self.repo.record_event("t1", "human", "cancel", {"note": "manual"})
        row = self.repo.conn.execute(
            "SELECT * FROM events WHERE task_id = 't1'"
        ).fetchone()
        self.assertEqual(row["actor"], "human")
        self.assertEqual(row["type"], "cancel")
        self.assertEqual(json.loads(row["payload"]), {"note": "manual"})

    def test_transaction_rolls_back_on_keyboard_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            with self.repo.transaction():
                self.repo.conn.execute("INSERT INTO meta(key, value) VALUES('k', 'v')")
                raise KeyboardInterrupt

        # 回滚后事务已关闭，可以立刻开启下一个事务
        with self.repo.transaction():
            self.repo.conn.execute("INSERT INTO meta(key, value) VALUES('k2', 'v2')")

        self.assertIsNone(self.repo.get_meta("k"))
        self.assertEqual(self.repo.get_meta("k2"), "v2")


if __name__ == "__main__":
    unittest.main()
