"""JSON 提取：围栏、前后解释、多对象与未转义引号的修复。"""

import unittest

from bitlink.jsonout import extract_json_object, find_json_object, iter_json_objects


class ExtractTest(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json_object('{"a": 1}'), {"a": 1})

    def test_fenced_object(self):
        text = '先说明一下：\n```json\n{"a": 1}\n```\n以上。'
        self.assertEqual(extract_json_object(text), {"a": 1})

    def test_first_object_wins(self):
        text = '{"a": 1}\n{"b": 2}'
        self.assertEqual(extract_json_object(text), {"a": 1})

    def test_predicate_skips_echoed_object(self):
        text = '任务卡 {"id": "T1", "title": "x"}\n报告 {"status": "completed"}'
        found = find_json_object(text, lambda item: "status" in item)
        self.assertEqual(found, {"status": "completed"})

    def test_unescaped_inner_quotes_are_repaired(self):
        text = '{"verdict":"pass","reason":"他说 "好" 然后走了","findings":[]}'
        found = extract_json_object(text)
        self.assertEqual(found["verdict"], "pass")
        self.assertEqual(found["reason"], '他说 "好" 然后走了')

    def test_literal_newline_inside_string_is_not_repaired(self):
        text = '{"a": "line1\nline2"}'
        self.assertIsNone(extract_json_object(text))

    def test_iter_yields_every_object(self):
        text = '{"a": 1} 中间 {"b": 2}'
        self.assertEqual([item for item in iter_json_objects(text)], [{"a": 1}, {"b": 2}])

    def test_garbage_returns_none(self):
        self.assertIsNone(extract_json_object("这里没有任何 JSON"))


if __name__ == "__main__":
    unittest.main()
