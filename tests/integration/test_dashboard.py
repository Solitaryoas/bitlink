import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from helpers.temp_repo import temp_repo
from bitlink import config as config_module
from bitlink import dashboard, db

SCRIPT_TITLE = "<script>alert(1)</script>"


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self._repo_ctx = temp_repo()
        self.project = self._repo_ctx.__enter__()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name

        self.repo = db.Repository(db.connect(self.data_dir))
        db.migrate(self.repo.conn)
        self.repo.set_meta("base_ref", config_module.git_head(self.project))
        self.repo.create_cards(
            [
                {
                    "id": "T",
                    "title": SCRIPT_TITLE,
                    "description": "描述文本",
                    "acceptance": "验收标准文本",
                    "blocked_by": [],
                    "max_attempts": 3,
                    "timeout_seconds": None,
                }
            ]
        )
        self.repo.claim_ready(1)
        self.repo.update_attempt(
            "T",
            1,
            phase="passed",
            report_summary="报告摘要ABC",
            exec_jsonl_path="C:/logs/events.jsonl",
            exec_session_id="exec-1",
            review_verdict="pass",
            review_reason="验收通过XYZ",
            review_jsonl_path="C:/logs/review.jsonl",
            review_session_id="review-1",
        )
        self.repo.transition("T", ["running"], "done")

        self.server = dashboard.make_server(self.data_dir, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
        self.repo.close()
        self._tmp.cleanup()
        self._repo_ctx.__exit__(None, None, None)

    def request(self, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            return response, response.read().decode("utf-8", errors="replace")
        finally:
            conn.close()

    def test_ac8_list_page_shows_task_and_auto_refresh(self):
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn('content="5"', body)
        self.assertIn("attempts", body)
        self.assertIn("报告摘要ABC", body)
        self.assertIn("done", body)

    def test_ac8_detail_page_shows_review_reason_and_logs(self):
        response, body = self.request("GET", "/tasks/T")
        self.assertEqual(response.status, 200)
        self.assertIn("验收标准文本", body)
        self.assertIn("验收通过XYZ", body)
        self.assertIn("C:/logs/events.jsonl", body)
        self.assertIn("C:/logs/review.jsonl", body)

    def test_ac8_json_list_api(self):
        response, body = self.request("GET", "/api/tasks")
        self.assertEqual(response.status, 200)
        self.assertIn("application/json", response.getheader("Content-Type"))
        payload = json.loads(body)
        self.assertEqual(len(payload["tasks"]), 1)
        task = payload["tasks"][0]
        self.assertEqual(task["id"], "T")
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["attempt_count"], 1)
        self.assertEqual(task["report_summary"], "报告摘要ABC")

    def test_ac8_json_detail_api(self):
        response, body = self.request("GET", "/api/tasks/T")
        self.assertEqual(response.status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["acceptance"], "验收标准文本")
        self.assertEqual(payload["blocked_by"], [])
        self.assertEqual(len(payload["attempts"]), 1)
        self.assertEqual(payload["attempts"][0]["review_verdict"], "pass")

    def test_unknown_path_returns_404(self):
        response, body = self.request("GET", "/nope")
        self.assertEqual(response.status, 404)

    def test_unknown_task_returns_404(self):
        response, _ = self.request("GET", "/tasks/missing")
        self.assertEqual(response.status, 404)
        response, _ = self.request("GET", "/api/tasks/missing")
        self.assertEqual(response.status, 404)

    def test_post_is_method_not_allowed(self):
        response, _ = self.request("POST", "/")
        self.assertEqual(response.status, 405)
        self.assertEqual(response.getheader("Allow"), "GET")

    def test_api_method_not_allowed_returns_json(self):
        response, body = self.request("POST", "/api/tasks")
        self.assertEqual(response.status, 405)
        self.assertEqual(response.getheader("Allow"), "GET")
        self.assertIn("application/json", response.getheader("Content-Type"))
        payload = json.loads(body)
        self.assertEqual(payload["error"], "method not allowed")
        self.assertEqual(payload["allow"], "GET")

    def test_put_patch_delete_return_405(self):
        """只读承诺要求所有写方法都被挡住，而不只是 POST。

        防的回归：删掉 do_PATCH/do_DELETE 后，BaseHTTPRequestHandler 会回
        501「Unsupported method」，调用方看到的是「服务不支持」而不是明确拒绝。
        """
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                response, _ = self.request(method, "/")
                self.assertEqual(response.status, 405)
                self.assertEqual(response.getheader("Allow"), "GET")

    def test_readonly_open_failure_returns_500_without_write_fallback(self):
        with mock.patch.object(
            dashboard.db, "connect", side_effect=sqlite3.OperationalError("readonly open failed")
        ):
            response, body = self.request("GET", "/api/tasks")
        self.assertEqual(response.status, 500)
        self.assertIn("数据库错误", body)

    def test_readonly_connection_is_query_only(self):
        conn = db.connect(self.data_dir, readonly=True)
        try:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO meta(key, value) VALUES('x', 'y')")
        finally:
            conn.close()

    def test_dashboard_open_readonly_rejects_insert(self):
        conn = dashboard._open_readonly(self.data_dir)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO meta(key, value) VALUES('dashboard', 'write')")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE tasks SET title = 'hacked' WHERE id = 'T'")
        finally:
            conn.close()
        self.assertEqual(self.repo.get_meta("dashboard"), None)

    def test_list_api_survives_daemon_closing_database(self):
        self.repo.close()
        try:
            response, body = self.request("GET", "/api/tasks")
            self.assertEqual(response.status, 200)
            payload = json.loads(body)
            self.assertEqual([task["id"] for task in payload["tasks"]], ["T"])
            response, body = self.request("GET", "/")
            self.assertEqual(response.status, 200)
            self.assertIn("T", body)
        finally:
            self.repo = db.Repository(db.connect(self.data_dir))

    def test_ac8_script_in_title_is_escaped(self):
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>alert", body)

    def test_page_has_no_external_resources(self):
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertNotIn("http://", body)
        self.assertNotIn("https://", body)

    def test_list_tasks_status_filter(self):
        self.repo.create_cards(
            [
                {
                    "id": "F",
                    "title": "failed task",
                    "description": "d",
                    "acceptance": "a",
                    "blocked_by": [],
                    "max_attempts": 3,
                    "timeout_seconds": None,
                }
            ]
        )
        self.repo.claim_ready(1)
        self.repo.transition("F", ["running"], "failed")
        self.repo.create_cards(
            [
                {
                    "id": "R",
                    "title": "ready task",
                    "description": "d",
                    "acceptance": "a",
                    "blocked_by": [],
                    "max_attempts": 3,
                    "timeout_seconds": None,
                }
            ]
        )

        conn = dashboard._open_readonly(self.data_dir)
        try:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(len(dashboard.list_tasks(conn)), 3)
            self.assertEqual(len(dashboard.list_tasks(conn, "")), 3)
            self.assertEqual([task["id"] for task in dashboard.list_tasks(conn, "failed")], ["F"])
            self.assertEqual([task["id"] for task in dashboard.list_tasks(conn, "done")], ["T"])
            self.assertEqual(dashboard.list_tasks(conn, "not-a-status"), [])
        finally:
            conn.close()

    def _seed_status_task(self, task_id, title, final_status):
        self.repo.create_cards(
            [
                {
                    "id": task_id,
                    "title": title,
                    "description": "d",
                    "acceptance": "a",
                    "blocked_by": [],
                    "max_attempts": 3,
                    "timeout_seconds": None,
                }
            ]
        )
        if final_status == "ready":
            return
        self.repo.claim_ready(1)
        self.repo.transition(task_id, ["running"], final_status)

    def test_list_api_status_filter_with_value(self):
        self._seed_status_task("F", "failed task", "failed")

        response, body = self.request("GET", "/api/tasks?status=failed")
        self.assertEqual(response.status, 200)
        tasks = json.loads(body)["tasks"]
        self.assertEqual([task["id"] for task in tasks], ["F"])
        for task in tasks:
            self.assertEqual(task["status"], "failed")

    def test_list_api_status_filter_unknown_value_returns_empty(self):
        self._seed_status_task("F", "failed task", "failed")

        response, body = self.request("GET", "/api/tasks?status=not-a-status")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body), {"tasks": []})
        self.assertEqual(body, '{"tasks": []}')

    def test_list_api_status_blank_equals_unfiltered(self):
        self._seed_status_task("F", "failed task", "failed")

        _, plain_body = self.request("GET", "/api/tasks")
        response, blank_body = self.request("GET", "/api/tasks?status=")
        self.assertEqual(response.status, 200)
        self.assertEqual(
            sorted(task["id"] for task in json.loads(blank_body)["tasks"]),
            sorted(task["id"] for task in json.loads(plain_body)["tasks"]),
        )

    def test_list_api_status_absent_returns_every_task(self):
        self._seed_status_task("F", "failed task", "failed")
        self._seed_status_task("R", "ready task", "ready")

        response, body = self.request("GET", "/api/tasks")
        self.assertEqual(response.status, 200)
        self.assertEqual(
            sorted(task["id"] for task in json.loads(body)["tasks"]), ["F", "R", "T"]
        )

    def test_html_list_status_filter_with_value(self):
        self._seed_status_task("F", "failed task", "failed")
        self._seed_status_task("R", "ready task", "ready")

        response, body = self.request("GET", "/?status=failed")
        self.assertEqual(response.status, 200)
        self.assertIn("failed task", body)
        self.assertNotIn("ready task", body)
        self.assertNotIn(SCRIPT_TITLE.replace("<", "&lt;").replace(">", "&gt;"), body)

    def test_html_list_status_unknown_and_blank(self):
        self._seed_status_task("F", "failed task", "failed")

        response, body = self.request("GET", "/?status=not-a-status")
        self.assertEqual(response.status, 200)
        self.assertNotIn("failed task", body)

        _, blank_body = self.request("GET", "/?status=")
        self.assertEqual(blank_body, self.request("GET", "/")[1])
        self.assertIn("failed task", blank_body)


if __name__ == "__main__":
    unittest.main()
