from __future__ import annotations

import unittest

from src.planning.engine import _unique_codes, _validate_destination_codes


class PlanningEngineKeyTests(unittest.TestCase):
    def test_unique_codes_preserves_workbook_order(self):
        rows = [
            {"A": "1301"},
            {"A": "1302"},
            {"A": "1301"},
            {"A": None},
        ]
        self.assertEqual(_unique_codes(rows), ["1301", "1302"])

    def test_destination_code_validation_fails_closed(self):
        with self.assertRaises(RuntimeError):
            _validate_destination_codes(
                ["1301", "1302"],
                Nokho=["1301", "1399"],
            )

    def test_destination_code_validation_accepts_different_order(self):
        _validate_destination_codes(
            ["1301", "1302"],
            Nokho=["1302", "1301"],
            Tinh_ung_hang=["1301", "1302"],
        )


if __name__ == "__main__":
    unittest.main()
