import unittest

from smoke_upgrade import preserves_values


class PreservedResponseTests(unittest.TestCase):
    def test_allows_additional_fields_at_any_depth(self):
        before = {"messages": [{"id": 1, "text": "Synthetic SMS"}], "count": 1}
        after = {
            "messages": [{"id": 1, "text": "Synthetic SMS", "forwarding": {"state": "sent"}}],
            "count": 1,
            "forwarding_enabled": True,
        }
        self.assertTrue(preserves_values(after, before))

    def test_rejects_missing_fields(self):
        self.assertFalse(preserves_values({"messages": [{}]}, {"messages": [{"text": "SMS"}]}))

    def test_rejects_changed_values(self):
        self.assertFalse(preserves_values({"text": "Changed"}, {"text": "Original"}))

    def test_rejects_changed_list_length_or_order(self):
        for actual in ([1], [1, 2, 3], [2, 1]):
            with self.subTest(actual=actual):
                self.assertFalse(preserves_values(actual, [1, 2]))

    def test_rejects_changed_types(self):
        for actual, expected in ((1, True), (None, ""), ([], {}), ("1", 1)):
            with self.subTest(actual=actual, expected=expected):
                self.assertFalse(preserves_values(actual, expected))
