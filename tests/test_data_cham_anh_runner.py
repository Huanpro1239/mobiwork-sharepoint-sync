import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import run_data_cham_anh as runner  # noqa: E402
from mobiwork import ReportConfig  # noqa: E402


class DataChamAnhRunnerTests(unittest.TestCase):
    def test_cross_month_targets_publish_once_per_month(self):
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
        target_dates = [
            date(2026, 9, 2),
            date(2026, 9, 1),
            date(2026, 8, 31),
        ]
        september = {"month": "2026-09", "filename": "Data_cham_anh_2026-09.xlsx"}
        august = {"month": "2026-08", "filename": "Data_cham_anh_2026-08.xlsx"}

        with patch.object(
            runner,
            "publish_data_cham_anh_month",
            side_effect=[september, august],
        ) as publish_mock:
            exports = runner.publish_target_months(
                reports,
                sharepoint=object(),
                drive_id="drive",
                target_dates=target_dates,
                dry_run=False,
            )

        self.assertEqual(exports, [september, august])
        self.assertEqual(publish_mock.call_count, 2)
        first_args = publish_mock.call_args_list[0].args
        second_args = publish_mock.call_args_list[1].args
        self.assertEqual(first_args[3], date(2026, 9, 2))
        self.assertEqual(second_args[3], date(2026, 8, 31))

    def test_dry_run_does_not_touch_sharepoint(self):
        reports = [
            ReportConfig(key="visit", enabled=True, name="visit", folder="01"),
            ReportConfig(key="bill", enabled=True, name="bill", folder="04"),
        ]

        with patch.object(runner, "publish_data_cham_anh_month") as publish_mock:
            exports = runner.publish_target_months(
                reports,
                sharepoint=None,
                drive_id=None,
                target_dates=[date(2026, 9, 3)],
                dry_run=True,
            )

        self.assertEqual(exports, [])
        publish_mock.assert_not_called()

    def test_same_month_targets_are_deduplicated_to_latest_anchor(self):
        anchors = runner.month_anchors(
            [date(2026, 9, 1), date(2026, 9, 3), date(2026, 9, 2)]
        )
        self.assertEqual(anchors, [date(2026, 9, 3)])

    def test_production_workflow_runs_combined_export_after_reports(self):
        workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "mobiwork-sync.yml"
        workflow = workflow_path.read_text(encoding="utf-8")

        report_command = "python src/run_all_reports.py"
        combined_command = "python src/run_data_cham_anh.py"
        self.assertIn(report_command, workflow)
        self.assertIn(combined_command, workflow)
        self.assertLess(workflow.index(report_command), workflow.index(combined_command))


if __name__ == "__main__":
    unittest.main()
