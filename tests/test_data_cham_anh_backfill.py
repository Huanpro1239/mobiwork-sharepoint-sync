import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import run_data_cham_anh_backfill as backfill  # noqa: E402


class DataChamAnhBackfillTests(unittest.TestCase):
    def test_parse_month_accepts_exact_year_month(self):
        self.assertEqual(
            backfill.parse_month("2026-09", label="from_month"),
            date(2026, 9, 1),
        )

    def test_parse_month_rejects_non_canonical_value(self):
        with self.assertRaises(ValueError):
            backfill.parse_month("2026-9", label="from_month")

    def test_month_range_is_inclusive_across_year_boundary(self):
        months = backfill.month_range(date(2026, 11, 1), date(2027, 2, 1))
        self.assertEqual(
            months,
            [
                date(2026, 11, 1),
                date(2026, 12, 1),
                date(2027, 1, 1),
                date(2027, 2, 1),
            ],
        )

    def test_month_range_rejects_reverse_range(self):
        with self.assertRaises(ValueError):
            backfill.month_range(date(2026, 9, 1), date(2026, 8, 1))

    def test_month_range_enforces_safety_limit(self):
        with self.assertRaises(ValueError):
            backfill.month_range(
                date(2026, 1, 1),
                date(2026, 3, 1),
                max_months=2,
            )

    def test_publish_backfill_writes_manifest(self):
        months = [date(2026, 8, 1), date(2026, 9, 1)]
        results = [
            {
                "status": "success",
                "month": "2026-08",
                "filename": "Data_cham_anh_2026-08.xlsx",
                "data_anh_rows": 10,
                "data_don_hang_rows": 20,
            },
            {
                "status": "success",
                "month": "2026-09",
                "filename": "Data_cham_anh_2026-09.xlsx",
                "data_anh_rows": 30,
                "data_don_hang_rows": 40,
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = root / "manifest.json"
            output_dir = root / "excel"
            with patch.object(
                backfill,
                "publish_data_cham_anh_month",
                side_effect=results,
            ) as publish_mock:
                actual = backfill.publish_backfill(
                    [],
                    sharepoint=object(),
                    drive_id="drive",
                    months=months,
                    output_dir=output_dir,
                    manifest_path=manifest_path,
                )

            self.assertEqual(actual, results)
            self.assertEqual(publish_mock.call_count, 2)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["requested_month_count"], 2)
            self.assertEqual(payload["successful_month_count"], 2)
            self.assertEqual(payload["failed_month_count"], 0)

    def test_permanent_workflow_calls_backfill_runner_and_shares_production_lock(self):
        workflow_path = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "data-cham-anh-backfill.yml"
        )
        workflow = workflow_path.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("python src/run_data_cham_anh_backfill.py", workflow)
        self.assertIn("group: mobiwork-sharepoint-production", workflow)
        self.assertIn('DATA_CHAM_ANH_ROOT_FOLDER: "05_DataChamAnh"', workflow)


if __name__ == "__main__":
    unittest.main()
