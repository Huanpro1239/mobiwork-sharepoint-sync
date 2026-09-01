import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import production_smoke as smoke  # noqa: E402


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


class ProductionSmokeImageStateTests(unittest.TestCase):
    def test_image_state_accepts_completed_clean_state(self):
        result = smoke.evaluate_image_state(
            {
                "last_completed_sync_date": "2026-09-01",
                "last_successful_sync_date": "2026-09-01",
                "failed_count": 0,
                "retry_from_date": None,
            },
            date(2026, 9, 1),
        )
        self.assertEqual(result["failed_count"], 0)

    def test_image_state_rejects_lagging_cursor(self):
        with self.assertRaisesRegex(AssertionError, "behind target date"):
            smoke.evaluate_image_state(
                {
                    "last_completed_sync_date": "2026-08-31",
                    "failed_count": 0,
                    "retry_from_date": None,
                },
                date(2026, 9, 1),
            )

    def test_image_state_rejects_unresolved_retry(self):
        with self.assertRaisesRegex(AssertionError, "unresolved work"):
            smoke.evaluate_image_state(
                {
                    "last_completed_sync_date": "2026-09-01",
                    "failed_count": 1,
                    "retry_from_date": "2026-08-31",
                },
                date(2026, 9, 1),
            )


if __name__ == "__main__":
    unittest.main()
