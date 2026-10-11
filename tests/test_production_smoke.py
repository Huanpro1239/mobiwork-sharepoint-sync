import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import production_smoke as smoke  # noqa: E402
from monthly_master import build_month_from_partitions, frames_from_records, read_master, write_master  # noqa: E402


class ProductionSmokeFrameTests(unittest.TestCase):
    def test_compare_report_frames_accepts_matching_partition(self):
        target = date(2026, 9, 1)
        expected = {
            "Data": pd.DataFrame(
                {
                    "_sync_date": ["2026-09-01", "2026-09-01"],
                    "ma_kh": ["A", "B"],
                    "qty": [1, 2],
                }
            )
        }
        actual = {
            "Data": pd.DataFrame(
                {
                    "_sync_date": ["2026-08-31", "2026-09-01", "2026-09-01"],
                    "ma_kh": ["OLD", "A", "B"],
                    "qty": [9, 1, 2],
                }
            )
        }

        result = smoke.compare_report_frames(actual, expected, target)
        self.assertEqual(result["compared_rows"], 2)

    def test_compare_report_frames_rejects_value_mismatch(self):
        target = date(2026, 9, 1)
        expected = {
            "Data": pd.DataFrame(
                {"_sync_date": ["2026-09-01"], "ma_kh": ["A"], "qty": [1]}
            )
        }
        actual = {
            "Data": pd.DataFrame(
                {"_sync_date": ["2026-09-01"], "ma_kh": ["A"], "qty": [99]}
            )
        }

        with self.assertRaisesRegex(AssertionError, "does not match fresh MobiWork source"):
            smoke.compare_report_frames(actual, expected, target)

    def test_compare_mixed_monthly_phone_column_to_numeric_looking_daily_phone(self):
        # Regression: before this fix, the monthly sheet read phones as strings
        # while the daily expected XLSX inferred numbers and removed leading zeroes.
        target = date(2026, 10, 10)
        monthly = build_month_from_partitions(
            [
                (date(2026, 10, 9), [{"ID": "old", "sdt": "HOTLINE"}]),
                (target, [{"ID": "0009", "sdt": "0984989830"}]),
            ],
            "flat",
            upsert_keys=["ID"],
        )
        daily = frames_from_records([{"ID": "0009", "sdt": "0984989830"}], "flat", target)
        with tempfile.TemporaryDirectory() as temporary:
            initial_dir = Path.cwd()
            try:
                os.chdir(temporary)
                actual_path = write_master(monthly, "Monthly", target)
                expected_path = write_master(daily, "Daily", target)
                actual = read_master(actual_path.read_bytes(), "flat")
                expected = read_master(expected_path.read_bytes(), "flat")
            finally:
                os.chdir(initial_dir)
        self.assertEqual(actual["Data"].iloc[1]["sdt"], "0984989830")
        self.assertEqual(expected["Data"].iloc[0]["sdt"], "0984989830")
        self.assertEqual(smoke.compare_report_frames(actual, expected, target)["compared_rows"], 1)

    def test_mismatch_still_fails_without_leaking_personal_values(self):
        target = date(2026, 10, 10)
        actual = {"Data": pd.DataFrame({
            "_sync_date": [target.isoformat()], "ID": ["0009"], "sdt": ["0984989830"],
        })}
        expected = {"Data": pd.DataFrame({
            "_sync_date": [target.isoformat()], "ID": ["0009"], "sdt": ["0984989831"],
        })}
        with self.assertRaises(AssertionError) as raised:
            smoke.compare_report_frames(actual, expected, target)
        message = str(raised.exception)
        self.assertIn("differing_columns=['sdt']", message)
        self.assertNotIn("0984989830", message)
        self.assertNotIn("0984989831", message)
        self.assertIn("does not match fresh MobiWork source", message)

    def test_compare_report_frames_rejects_stale_extra_values(self):
        target = date(2026, 9, 1)
        expected = {
            "Data": pd.DataFrame(
                {"_sync_date": ["2026-09-01"], "ma_kh": ["A"]}
            )
        }
        actual = {
            "Data": pd.DataFrame(
                {
                    "_sync_date": ["2026-09-01"],
                    "ma_kh": ["A"],
                    "removed_field": ["stale"],
                }
            )
        }

        with self.assertRaisesRegex(AssertionError, "stale extra values"):
            smoke.compare_report_frames(actual, expected, target)


if __name__ == "__main__":
    unittest.main()
