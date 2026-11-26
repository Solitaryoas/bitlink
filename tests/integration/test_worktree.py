import os
import subprocess
import sys
import tempfile
import unittest

from helpers.temp_repo import git, temp_repo
from bitlink import running, worktree


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        self._repo_ctx = temp_repo()
        self.repo_path = self._repo_ctx.__enter__()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        self.worktrees_dir = os.path.join(self._tmp.name, "worktrees")
        self.base_ref = git(self.repo_path, "rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        self._tmp.cleanup()
        self._repo_ctx.__exit__(None, None, None)

    def test_ac3_worktree_is_isolated_and_main_worktree_stays_clean(self):
        path = worktree.ensure_worktree(
            self.repo_path, "task1", self.base_ref, self.worktrees_dir
        )
        self.assertTrue(os.path.isdir(path))
        self.assertNotIn(self.repo_path, os.path.abspath(path))

        main_status = git(self.repo_path, "status", "--porcelain").stdout.strip()
        self.assertEqual(main_status, "")

        listing = git(self.repo_path, "worktree", "list").stdout.strip().splitlines()
        self.assertEqual(len(listing), 2)

        branches = git(self.repo_path, "branch", "--list", "bitlink/task1").stdout
        self.assertIn("bitlink/task1", branches)

        with open(os.path.join(path, "note.txt"), "w", encoding="utf-8") as handle:
            handle.write("hello\n")
        self.assertEqual(git(self.repo_path, "status", "--porcelain").stdout.strip(), "")

    def test_ensure_worktree_reuses_existing_directory(self):
        first = worktree.ensure_worktree(
            self.repo_path, "task1", self.base_ref, self.worktrees_dir
        )
        second = worktree.ensure_worktree(
            self.repo_path, "task1", self.base_ref, self.worktrees_dir
        )
        self.assertEqual(first, second)
        listing = git(self.repo_path, "worktree", "list").stdout.strip().splitlines()
        self.assertEqual(len(listing), 2)

    def test_make_diff_captures_changes(self):
        path = worktree.ensure_worktree(
            self.repo_path, "task1", self.base_ref, self.worktrees_dir
        )
        with open(os.path.join(path, "answer.txt"), "w", encoding="utf-8") as handle:
            handle.write("42\n")
        diff_path = os.path.join(self._tmp.name, "diff.patch")
        worktree.make_diff(path, self.base_ref, diff_path)
        with open(diff_path, "r", encoding="utf-8", errors="replace") as handle:
            patch = handle.read()
        self.assertIn("answer.txt", patch)
        self.assertIn("+42", patch)

    def test_chain_base_prefers_dependency_tip(self):
        first = worktree.ensure_worktree(
            self.repo_path, "A", self.base_ref, self.worktrees_dir
        )
        with open(os.path.join(first, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("a\n")
        sha = worktree.commit_worktree(first, "task A")
        self.assertEqual(len(sha), 40)

        base = worktree.resolve_chain_base(self.repo_path, self.base_ref, ["A"])
        self.assertEqual(base, sha)

        second = worktree.ensure_worktree(self.repo_path, "B", base, self.worktrees_dir)
        self.assertTrue(os.path.isfile(os.path.join(second, "a.txt")))

    def test_diverged_dependencies_raise(self):
        for task_id in ("A", "B"):
            path = worktree.ensure_worktree(
                self.repo_path, task_id, self.base_ref, self.worktrees_dir
            )
            with open(os.path.join(path, f"{task_id}.txt"), "w", encoding="utf-8") as handle:
                handle.write("x\n")
            worktree.commit_worktree(path, f"task {task_id}")
        with self.assertRaises(worktree.WorktreeError) as ctx:
            worktree.resolve_chain_base(self.repo_path, self.base_ref, ["A", "B"])
        self.assertEqual(ctx.exception.reason, "chain_diverged")

    def test_commit_worktree_without_changes_returns_head(self):
        path = worktree.ensure_worktree(
            self.repo_path, "task1", self.base_ref, self.worktrees_dir
        )
        head = git(path, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(worktree.commit_worktree(path, "empty"), head)

    def test_remove_worktree_cleans_directory_and_keeps_branch(self):
        path = worktree.ensure_worktree(
            self.repo_path, "task1", self.base_ref, self.worktrees_dir
        )
        self.assertTrue(worktree.remove_worktree(self.repo_path, path))
        self.assertFalse(os.path.isdir(path))
        listing = git(self.repo_path, "worktree", "list").stdout.strip().splitlines()
        self.assertEqual(len(listing), 1)
        branches = git(self.repo_path, "branch", "--list", "bitlink/task1").stdout
        self.assertIn("bitlink/task1", branches)

    def test_missing_base_ref_raises(self):
        with self.assertRaises(worktree.WorktreeError):
            worktree.ensure_worktree(
                self.repo_path, "task1", "0" * 40, self.worktrees_dir
            )

    def test_existing_directory_that_is_not_a_worktree_is_reported_in_use(self):
        path = os.path.join(self.worktrees_dir, "task1")
        os.makedirs(path)
        with self.assertRaises(worktree.WorktreeError) as ctx:
            worktree.ensure_worktree(
                self.repo_path, "task1", self.base_ref, self.worktrees_dir
            )
        self.assertEqual(ctx.exception.reason, "worktree_in_use")

    def test_reuse_is_rejected_while_running_marker_pid_is_alive(self):
        worktree.ensure_worktree(
            self.repo_path, "task1", self.base_ref, self.worktrees_dir, data_dir=self.data_dir
        )
        writer = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        try:
            running.write_marker(self.data_dir, "task1", pid=writer.pid)
            with self.assertRaises(worktree.WorktreeError) as ctx:
                worktree.ensure_worktree(
                    self.repo_path,
                    "task1",
                    self.base_ref,
                    self.worktrees_dir,
                    data_dir=self.data_dir,
                )
            self.assertEqual(ctx.exception.reason, "worktree_in_use")
            self.assertTrue(os.path.isfile(running.marker_path(self.data_dir, "task1")))
        finally:
            writer.kill()
            writer.wait()

    def test_reuse_after_marker_writer_exits_clears_marker(self):
        first = worktree.ensure_worktree(
            self.repo_path, "task1", self.base_ref, self.worktrees_dir, data_dir=self.data_dir
        )
        writer = subprocess.Popen([sys.executable, "-c", "pass"])
        writer.wait()
        running.write_marker(self.data_dir, "task1", pid=writer.pid)

        second = worktree.ensure_worktree(
            self.repo_path, "task1", self.base_ref, self.worktrees_dir, data_dir=self.data_dir
        )

        self.assertEqual(first, second)
        self.assertFalse(os.path.isfile(running.marker_path(self.data_dir, "task1")))

    def test_stale_marker_is_cleared_before_reuse(self):
        first = worktree.ensure_worktree(
            self.repo_path, "task1", self.base_ref, self.worktrees_dir, data_dir=self.data_dir
        )
        marker = running.marker_path(self.data_dir, "task1")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write('{"pid": 999999, "task_id": "task1", "started_at": "x"}')

        second = worktree.ensure_worktree(
            self.repo_path, "task1", self.base_ref, self.worktrees_dir, data_dir=self.data_dir
        )

        self.assertEqual(first, second)
        self.assertFalse(os.path.isfile(marker))


if __name__ == "__main__":
    unittest.main()
