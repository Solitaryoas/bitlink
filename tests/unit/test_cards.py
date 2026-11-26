import os
import tempfile
import unittest

from bitlink import cards


class CardsTest(unittest.TestCase):
    def test_single_valid_card(self):
        parsed = cards.parse_cards([{"title": "t", "description": "d", "acceptance": "a"}])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(len(parsed[0]["id"]), 32)
        self.assertEqual(parsed[0]["blocked_by"], [])
        self.assertEqual(parsed[0]["max_attempts"], 3)
        self.assertIsNone(parsed[0]["timeout_seconds"])

    def test_accepts_wrapped_object(self):
        parsed = cards.parse_cards(
            {"cards": [{"title": "t", "description": "d", "acceptance": "a"}]}
        )
        self.assertEqual(len(parsed), 1)

    def test_batch_cards_can_reference_each_other(self):
        parsed = cards.parse_cards(
            [
                {"id": "a", "title": "t", "description": "d", "acceptance": "a",
                 "blocked_by": ["b"]},
                {"id": "b", "title": "t", "description": "d", "acceptance": "a"},
            ]
        )
        self.assertEqual([c["id"] for c in parsed], ["a", "b"])
        self.assertEqual(parsed[0]["blocked_by"], ["b"])

    def test_existing_dependency_is_allowed(self):
        parsed = cards.parse_cards(
            [{"title": "t", "description": "d", "acceptance": "a", "blocked_by": ["old"]}],
            existing_ids={"old"},
        )
        self.assertEqual(parsed[0]["blocked_by"], ["old"])

    def test_unknown_dependency_is_rejected_with_card_index(self):
        with self.assertRaises(cards.CardError) as ctx:
            cards.parse_cards(
                [
                    {"title": "t", "description": "d", "acceptance": "a"},
                    {"title": "t", "description": "d", "acceptance": "a",
                     "blocked_by": ["missing"]},
                ]
            )
        self.assertIn("第 2 张卡", str(ctx.exception))
        self.assertIn("missing", str(ctx.exception))

    def test_self_reference_is_rejected(self):
        with self.assertRaises(cards.CardError) as ctx:
            cards.parse_cards(
                [{"id": "a", "title": "t", "description": "d", "acceptance": "a",
                  "blocked_by": ["a"]}]
            )
        self.assertIn("自身", str(ctx.exception))

    def test_two_card_cycle_is_rejected(self):
        with self.assertRaises(cards.CardError) as ctx:
            cards.parse_cards(
                [
                    {"id": "a", "title": "t", "description": "d", "acceptance": "a",
                     "blocked_by": ["b"]},
                    {"id": "b", "title": "t", "description": "d", "acceptance": "a",
                     "blocked_by": ["a"]},
                ]
            )
        self.assertIn("环", str(ctx.exception))

    def test_missing_acceptance_is_rejected(self):
        with self.assertRaises(cards.CardError) as ctx:
            cards.parse_cards([{"title": "t", "description": "d", "acceptance": "  "}])
        self.assertIn("第 1 张卡", str(ctx.exception))
        self.assertIn("acceptance", str(ctx.exception))

    def test_duplicate_batch_id_is_rejected(self):
        with self.assertRaises(cards.CardError):
            cards.parse_cards(
                [
                    {"id": "a", "title": "t", "description": "d", "acceptance": "a"},
                    {"id": "a", "title": "t", "description": "d", "acceptance": "a"},
                ]
            )

    def test_id_with_path_separators_or_traversal_is_rejected(self):
        for bad in ("..", "../escape", "a/b", "a\\b", "x" * 65, ""):
            with self.subTest(card_id=bad):
                with self.assertRaises(cards.CardError):
                    cards.parse_cards(
                        [{"id": bad, "title": "t", "description": "d", "acceptance": "a"}]
                    )

    def test_id_with_allowed_characters_is_accepted(self):
        parsed = cards.parse_cards(
            [{"id": "Task_01-abc", "title": "t", "description": "d", "acceptance": "a"}]
        )
        self.assertEqual(parsed[0]["id"], "Task_01-abc")

    def test_id_already_in_database_is_rejected(self):
        with self.assertRaises(cards.CardError):
            cards.parse_cards(
                [{"id": "a", "title": "t", "description": "d", "acceptance": "a"}],
                existing_ids={"a"},
            )

    def test_wrong_type_is_rejected(self):
        with self.assertRaises(cards.CardError):
            cards.parse_cards([{"title": 1, "description": "d", "acceptance": "a"}])
        with self.assertRaises(cards.CardError):
            cards.parse_cards(
                [{"title": "t", "description": "d", "acceptance": "a", "max_attempts": 0}]
            )

    def test_empty_list_is_rejected(self):
        with self.assertRaises(cards.CardError):
            cards.parse_cards([])

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(cards.CardError):
            cards.parse_cards(
                [{"title": "t", "description": "d", "acceptance": "a", "owner": "x"}]
            )

    def test_load_cards_file_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cards.json")
            with open(path, "wb") as handle:
                handle.write(
                    b'\xef\xbb\xbf[{"title": "t", "description": "d", "acceptance": "a"}]'
                )
            parsed = cards.load_cards_file(path)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["title"], "t")


if __name__ == "__main__":
    unittest.main()
