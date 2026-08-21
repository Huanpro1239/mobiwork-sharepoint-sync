import unittest

from src.mobiwork import get_by_path


class GetByPathTests(unittest.TestCase):
    def test_nested_path(self):
        payload = {"data": {"items": [{"id": 1}]}}
        self.assertEqual(get_by_path(payload, "data.items"), [{"id": 1}])

    def test_missing_path(self):
        self.assertIsNone(get_by_path({"data": {}}, "data.items"))

    def test_empty_path_returns_payload(self):
        payload = {"ok": True}
        self.assertIs(get_by_path(payload, None), payload)


if __name__ == "__main__":
    unittest.main()
