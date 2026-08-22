import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import run_all_reports as runner  # noqa: E402
from mobiwork import ReportConfig  # noqa: E402


class FakeMobiWork:
    def __init__(self):
        self.calls = []

    def fetch_report(self, cfg, target_date):
        self.calls.append(cfg.key)
        return [{"report": cfg.key}]


class FakeSharePoint:
    def __init__(self, fail_report="visit"):
        self.fail_report = fail_report
        self.calls = []

    def upload_file(self, drive_id, path, remote_folder):
        report_key = Path(path).stem.split("_")[0]
        self.calls.append((report_key, remote_folder))
        if report_key == self.fail_report:
            raise RuntimeError("simulated SharePoint failure")
        return {
            "size": Path(path).stat().st_size + 10,
            "local_size": Path(path).stat().st_size,
            "verification_mode": "xlsx_semantic",
            "semantic_match": True,
            "webUrl": f"https://example/{Path(path).name}",
        }


class IncrementalScopeTests(unittest.TestCase):
    def test_today_scope_targets_current_vietnam_date(self):
        fixed_now = datetime(2026, 8, 22, 12, 30, tzinfo=runner.core.VN_TZ)
        with patch.object(runner, "datetime") as datetime_mock:
            datetime_mock.now.return_value = fixed_now
            values = runner.incremental_target_dates("today", 1)
        self.assertEqual(values, [date(2026, 8, 22)])

    def test_yesterday_scope_targets_previous_vietnam_date(self):
        fixed_now = datetime(2026, 8, 22, 0, 5, tzinfo=runner.core.VN_TZ)
        with patch.object(runner, "datetime") as datetime_mock:
            datetime_mock.now.return_value = fixed_now
            values = runner.incremental_target_dates("yesterday", 1)
        self.assertEqual(values, [date(2026, 8, 21)])

    def test_lookback_scope_reuses_core_date_logic(self):
        expected = [date(2026, 8, 21), date(2026, 8, 20)]
        with patch.object(runner.core, "target_dates", return_value=expected) as target_mock:
            values = runner.incremental_target_dates("lookback", 2)
        self.assertEqual(values, expected)
        target_mock.assert_called_once_with(2)

    def test_invalid_scope_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "SYNC_SCOPE"):
            runner.incremental_target_dates("invalid", 1)


class AllReportsRunnerTests(unittest.TestCase):
    def test_one_report_failure_does_not_block_remaining_reports(self):
        reports = [
            ReportConfig(key="visit", enabled=True, name="visit", folder="01"),
            ReportConfig(key="new_customer", enabled=True, name="new_customer", folder="02"),
            ReportConfig(key="order", enabled=True, name="order", folder="03"),
            ReportConfig(key="bill", enabled=True, name="bill", folder="04"),
        ]
        mobiwork = FakeMobiWork()
        sharepoint = FakeSharePoint(fail_report="visit")
        manifest = runner.core._new_manifest("incremental", False)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            def fake_export(records, name, target_date, export_mode):
                path = temp_path / f"{name}_{target_date.isoformat()}.xlsx"
                path.write_bytes(f"workbook-{name}".encode())
                return path

            with (
                patch.object(runner.core, "target_dates", return_value=[date(2026, 8, 21)]),
                patch.object(runner, "export_excel", side_effect=fake_export),
            ):
                results = runner.run_incremental_all_reports(
                    reports,
                    mobiwork,
                    sharepoint,
                    "drive",
                    1,
                    False,
                    manifest,
                    sync_scope="lookback",
                )

        self.assertEqual(mobiwork.calls, ["visit", "new_customer", "order", "bill"])
        self.assertEqual(len(sharepoint.calls), 4)
        self.assertEqual(len(results), 4)
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual([item["status"] for item in results[1:]], ["success"] * 3)
        self.assertEqual(len(manifest["files"]), 3)

        runner._finalize_manifest(manifest, results)
        self.assertEqual(manifest["status"], "partial_failure")
        self.assertEqual(manifest["failed_report_count"], 1)
        self.assertEqual(manifest["successful_report_count"], 3)

    def test_all_success_is_marked_success(self):
        results = [
            {"report": "visit", "target_date": "2026-08-21", "status": "success"},
            {"report": "bill", "target_date": "2026-08-21", "status": "success"},
        ]
        manifest = runner.core._new_manifest("incremental", True)

        runner._finalize_manifest(manifest, results)

        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["failed_report_count"], 0)
        self.assertEqual(manifest["successful_report_count"], 2)


if __name__ == "__main__":
    unittest.main()
