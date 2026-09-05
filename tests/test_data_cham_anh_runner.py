import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import run_all_reports as runner  # noqa: E402
from mobiwork import ReportConfig  # noqa: E402


class DataChamAnhRunnerTests(unittest.TestCase):
    def test_successful_visit_and_bill_month_publishes_one_combined_workbook(self):
        reports = [
            ReportConfig(
                key="visit",
                enabled=True,
                name="BaoCaoViengTham",
                folder="01_BaoCaoViengTham",
            ),
            ReportConfig(
                key="bill",
                enabled=True,
                name="DonBanHang",
                folder="04_DonBanHang",
            ),
        ]
        results = [
            {"report": "visit", "target_date": "2026-09-03", "status": "success"},
            {"report": "bill", "target_date": "2026-09-03", "status": "success"},
        ]
        manifest = runner.core._new_manifest("incremental", False)
        expected = {
            "month": "2026-09",
            "filename": "Data_cham_anh_2026-09.xlsx",
            "data_anh_rows": 10,
            "data_don_hang_rows": 20,
        }

        with patch.object(
            runner,
            "publish_data_cham_anh_month",
            return_value=expected,
        ) as publish_mock:
            exports = runner.publish_data_cham_anh_exports(
                reports,
                results,
                sharepoint=object(),
                drive_id="drive",
                dry_run=False,
                manifest=manifest,
            )

        self.assertEqual(exports, [expected])
        publish_mock.assert_called_once()
        args = publish_mock.call_args.args
        self.assertIs(args[0], reports)
        self.assertEqual(args[2], "drive")
        self.assertEqual(args[3], date(2026, 9, 3))
        self.assertEqual(manifest["data_cham_anh_exports"], [expected])
        self.assertEqual(manifest["data_cham_anh_export_count"], 1)

    def test_combined_workbook_is_skipped_when_bill_month_failed(self):
        reports = [
            ReportConfig(key="visit", enabled=True, name="visit", folder="01"),
            ReportConfig(key="bill", enabled=True, name="bill", folder="04"),
        ]
        results = [
            {"report": "visit", "target_date": "2026-09-03", "status": "success"},
            {"report": "bill", "target_date": "2026-09-03", "status": "failed"},
        ]
        manifest = runner.core._new_manifest("incremental", False)

        with patch.object(runner, "publish_data_cham_anh_month") as publish_mock:
            exports = runner.publish_data_cham_anh_exports(
                reports,
                results,
                sharepoint=object(),
                drive_id="drive",
                dry_run=False,
                manifest=manifest,
            )

        self.assertEqual(exports, [])
        publish_mock.assert_not_called()
        self.assertEqual(manifest["data_cham_anh_exports"], [])

    def test_combined_workbook_is_not_uploaded_in_dry_run(self):
        reports = [
            ReportConfig(key="visit", enabled=True, name="visit", folder="01"),
            ReportConfig(key="bill", enabled=True, name="bill", folder="04"),
        ]
        results = [
            {"report": "visit", "target_date": "2026-09-03", "status": "success"},
            {"report": "bill", "target_date": "2026-09-03", "status": "success"},
        ]
        manifest = runner.core._new_manifest("incremental", True)

        with patch.object(runner, "publish_data_cham_anh_month") as publish_mock:
            exports = runner.publish_data_cham_anh_exports(
                reports,
                results,
                sharepoint=None,
                drive_id=None,
                dry_run=True,
                manifest=manifest,
            )

        self.assertEqual(exports, [])
        publish_mock.assert_not_called()
        self.assertEqual(manifest["data_cham_anh_exports"], [])


if __name__ == "__main__":
    unittest.main()
