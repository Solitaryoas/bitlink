"""规划层：提示构造、仓库事实采集、key→id 映射与输出解析。"""

import json
import os
import sys
import tempfile
import unittest

from helpers.temp_repo import git, temp_repo
from bitlink import planner

FAKE_PLANNER_SOURCE = r'''
import json
import os
import sys

args = sys.argv[1:]
log_path = os.environ.get("FAKE_PLAN_ARGV_LOG")
if log_path:
    with open(log_path, "w", encoding="utf-8") as handle:
        json.dump(args, handle)

out_path = None
for index, value in enumerate(args):
    if value == "-o":
        out_path = args[index + 1]

print(json.dumps({"type": "thread.started", "thread_id": "plan-session-1"}), flush=True)

body = os.environ.get("FAKE_PLAN_BODY")
if out_path and body is not None:
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(body)

sys.exit(int(os.environ.get("FAKE_PLAN_EXIT", "0") or 0))
'''


def card(key, **overrides):
    payload = {
        "key": key,
        "title": f"任务 {key}",
        "description": "描述",
        "acceptance": "python3 -m unittest discover -s tests",
    }
    payload.update(overrides)
    return payload


class NormalizePlanTest(unittest.TestCase):
    def test_keys_become_uuids_and_dependencies_are_rewritten(self):
        cards, warnings = planner.normalize_plan(
            {"cards": [card("T1"), card("T2", blocked_by=["T1"])]}
        )
        self.assertEqual(warnings, [])
        self.assertEqual(len(cards), 2)
        first, second = cards
        self.assertNotEqual(first["id"], "T1")
        self.assertEqual(len(first["id"]), 32)
        self.assertEqual(second["blocked_by"], [first["id"]])

    def test_existing_task_id_can_be_used_as_dependency(self):
        cards, _ = planner.normalize_plan(
            {"cards": [card("T1", blocked_by=["existingtask"])]},
            existing_ids=["existingtask"],
        )
        self.assertEqual(cards[0]["blocked_by"], ["existingtask"])

    def test_unknown_dependency_is_rejected(self):
        with self.assertRaises(planner.PlanError):
            planner.normalize_plan({"cards": [card("T1", blocked_by=["T9"])]})

    def test_duplicate_key_is_rejected(self):
        with self.assertRaises(planner.PlanError):
            planner.normalize_plan({"cards": [card("T1"), card("T1")]})

    def test_cycle_is_rejected(self):
        with self.assertRaises(ValueError):
            planner.normalize_plan(
                {"cards": [card("T1", blocked_by=["T2"]), card("T2", blocked_by=["T1"])]}
            )

    def test_unknown_fields_are_dropped_with_warning(self):
        cards, warnings = planner.normalize_plan(
            {"cards": [dict(card("T1"), priority="high")]}
        )
        self.assertEqual(len(cards), 1)
        self.assertTrue(any("priority" in warning for warning in warnings))

    def test_too_many_cards_is_rejected(self):
        too_many = {"cards": [card(f"T{i}") for i in range(1, planner.MAX_CARDS + 2)]}
        with self.assertRaises(planner.PlanError):
            planner.normalize_plan(too_many)

    def test_missing_acceptance_is_rejected(self):
        broken = card("T1")
        broken.pop("acceptance")
        with self.assertRaises(ValueError):
            planner.normalize_plan({"cards": [broken]})


class PromptTest(unittest.TestCase):
    def test_prompt_carries_request_facts_and_existing_titles(self):
        prompt = planner.build_plan_prompt(
            "给登录加失败次数限制",
            "共 2 个受控文件：\n- a.py\n- b.py",
            ["已有任务"],
            "项目约定：用中文注释",
        )
        self.assertIn("给登录加失败次数限制", prompt)
        self.assertIn("a.py", prompt)
        self.assertIn("已有任务", prompt)
        self.assertIn("项目约定：用中文注释", prompt)
        self.assertIn("不要执行 git 命令", prompt)
        self.assertIn("acceptance", prompt)


class CollectFactsTest(unittest.TestCase):
    def test_lists_tracked_files(self):
        with temp_repo() as repo_path:
            with open(os.path.join(repo_path, "tracked.py"), "w", encoding="utf-8") as handle:
                handle.write("x = 1\n")
            git(repo_path, "add", "tracked.py")
            facts = planner.collect_facts(repo_path)
        self.assertIn("tracked.py", facts)

    def test_truncates_long_file_lists(self):
        with temp_repo() as repo_path:
            for index in range(5):
                with open(os.path.join(repo_path, f"f{index}.txt"), "w", encoding="utf-8") as handle:
                    handle.write("x\n")
            git(repo_path, "add", ".")
            facts = planner.collect_facts(repo_path, limit=2)
        self.assertIn("省略", facts)
        self.assertNotIn("f4.txt", facts)


class PlannerProcessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.repo = os.path.join(self.dir, "repo")
        os.makedirs(self.repo)
        self.script = os.path.join(self.dir, "fake_planner.py")
        with open(self.script, "w", encoding="utf-8") as handle:
            handle.write(FAKE_PLANNER_SOURCE)
        self.out = os.path.join(self.dir, "plans", "draft.json")
        self.raw = os.path.join(self.dir, "plans", "draft.raw.json")
        self.argv_log = os.path.join(self.dir, "argv.json")

    def tearDown(self):
        self._tmp.cleanup()

    def make_planner(self, **env):
        base = {"FAKE_PLAN_ARGV_LOG": self.argv_log}
        base.update(env)
        return planner.Planner(
            executable=[sys.executable, self.script],
            project_root=self.repo,
            env=base,
        )

    def test_command_is_read_only_and_writes_draft(self):
        body = json.dumps({"cards": [card("T1")]})
        instance = self.make_planner(FAKE_PLAN_BODY=body)
        result = instance.plan("加个功能", self.out, self.raw, 30)
        self.assertEqual(len(result["cards"]), 1)
        with open(self.argv_log, "r", encoding="utf-8") as handle:
            argv = json.load(handle)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("--output-schema", argv)
        with open(self.out, "r", encoding="utf-8") as handle:
            draft = json.load(handle)
        self.assertEqual(len(draft["cards"]), 1)

    def test_prose_wrapped_json_is_accepted(self):
        body = "先拆成一张卡：\n" + json.dumps({"cards": [card("T1")]})
        instance = self.make_planner(FAKE_PLAN_BODY=body)
        result = instance.plan("加个功能", self.out, self.raw, 30)
        self.assertEqual(len(result["cards"]), 1)

    def test_missing_cards_array_is_reported(self):
        instance = self.make_planner(FAKE_PLAN_BODY='{"plan": "nothing"}')
        with self.assertRaises(planner.PlanError):
            instance.plan("加个功能", self.out, self.raw, 30)

    def test_non_zero_exit_is_reported(self):
        instance = self.make_planner(FAKE_PLAN_BODY="{}", FAKE_PLAN_EXIT="3")
        with self.assertRaises(planner.PlanError):
            instance.plan("加个功能", self.out, self.raw, 30)


if __name__ == "__main__":
    unittest.main()
