import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import main as sync_main  # noqa: E402
from mobiwork import ReportConfig  # noqa: E402


class FakeMobiWork:
    def __init__(self, daily_records=None, range_records=None):
        self.daily_records = list(daily_records or [])
        self.range_records = list(range_records or [])
        self.daily_calls = []
        self.range_calls = []

    def fetch_report(self, cfg, target_date):
        self.daily_calls.append((cfg.key, target_date))
        return list(self.daily_records)

    def fetch_report_range(self, cfg, from_date, to_date):
        self.range_calls.append((cfg.key, from_date, to_date))
        return list(self.range_records)


class FakeSharePoint:
    def __init__(self, state=None):
        self.state = state
        self.uploaded_files = []
        self.uploaded_json = []

    def upload_file(self, drive_id, path, remote_folder):
        self.uploaded_files.append((drive_id, Path(path), remote_folder))
        return {
            "size": Path(path).stat().st_size,
            "webUrl": f"https://sharepoint.example/{remote_folder}/{Path(path).name}",
        }

    def download_json(self, drive_id, remote_path):
        return self.state

    def upload_json(self, drive_id, remote_path, payload):
        self.uploaded_json.append((drive_id, remote_path, dict(payload)))
        self.state = dict(payload)
        return {"size": len(json.dumps(payload))}


class MainHelperTests(unittest.TestCase):
    def test_load_reports_and_enabled_filter(self):
        payload = {
            "reports": [
                {
                    "key": "a",
                    "enabled": True,
                    "name": "A",
                    "folder": "A",
                },
                {
                    "key": "b",
                    "enabled": False,
                    "name": "B",
                    "folder": "B",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "reports.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            reports = sync_main.load_reports(path)
        self.assertEqual([item.key for item in reports], ["a", "b"])

    def test_target_dates_and_validation(self):
        values = sync_main.target_dates(3)
        self.assertEqual(len(values), 3)
        self.assertEqual(values[1], values[0] - timedelta(days=1))
        with self.assertRaises(ValueError):
            sync_main.target_dates(0)
        with self.assertRaises(ValueError):
            sync_main.target_dates(32)

    def test_parse_iso_date(self):
        self.assertEqual(sync_main.parse_iso_date("2026-08-22", "x"), date(2026, 8, 22))
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            sync_main.parse_iso_date("22/08/2026", "x")

    def test_manifest_helpers_write_hash_and_totals(self):
        cfg = ReportConfig(key="bill", enabled=True, name="Bill", folder="04")
        manifest = sync_main._new_manifest("incremental", True)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Bill.xlsx"
            path.write_bytes(b"excel-bytes")
            sync_main._record_export(manifest, cfg, path, 7, None, None)
            self.assertEqual(manifest["files"][0]["source_rows"], 7)
            self.assertEqual(len(manifest["files"][0]["sha256"]), 64)

    def test_bootstrap_signature_and_state_round_trip(self):
        reports = [ReportConfig(key="bill", enabled=True, name="Bill", folder="04")]
        signature = sync_main._bootstrap_signature(reports, date(2020, 1, 1), 24)
        sharepoint = FakeSharePoint()

        self.assertIsNone(
            sync_main._load_bootstrap_state(sharepoint, "drive", signature, False)
        )
        sync_main._save_bootstrap_state(
            sharepoint,
            "drive",
            signature,
            date(2019, 12, 31),
            2,
            False,
        )
        state = sync_main._load_bootstrap_state(
            sharepoint,
            "drive",
            signature,
            False,
        )
        self.assertEqual(state["next_cursor_end"], "2019-12-31")
        self.assertIsNone(
            sync_main._load_bootstrap_state(sharepoint, "drive", signature, True)
        )

    def test_changed_bootstrap_signature_is_ignored(self):
        sharepoint = FakeSharePoint(
            state={"signature": {"report_keys": ["old"]}, "completed": False}
        )
        state = sync_main._load_bootstrap_state(
            sharepoint,
            "drive",
            {"report_keys": ["new"]},
            False,
        )
        self.assertIsNone(state)


class IncrementalTests(unittest.TestCase):
    def test_incremental_exports_uploads_and_records_manifest(self):
        cfg = ReportConfig(key="bill", enabled=True, name="DonBanHang", folder="04_DonBanHang")
        mobiwork = FakeMobiWork(daily_records=[{"ma_phieu": "A"}])
        sharepoint = FakeSharePoint()
        manifest = sync_main._new_manifest("incremental", False)

        with tempfile.TemporaryDirectory() as temp_dir:
            export_path = Path(temp_dir) / "DonBanHang_2026-08-21.xlsx"
            export_path.write_bytes(b"workbook")
            with (
                patch.object(sync_main, "target_dates", return_value=[date(2026, 8, 21)]),
                patch.object(sync_main, "export_excel", return_value=export_path),
            ):
                sync_main.run_incremental(
                    [cfg],
                    mobiwork,
                    sharepoint,
                    "drive",
                    1,
                    False,
                    manifest,
                )

        self.assertEqual(len(sharepoint.uploaded_files), 1)
        self.assertEqual(len(manifest["files"]), 1)
        self.assertEqual(manifest["files"][0]["source_rows"], 1)
        self.assertEqual(
            manifest["files"][0]["remote_folder"],
            "04_DonBanHang/2026/08",
        )

    def test_incremental_dry_run_skips_sharepoint(self):
        cfg = ReportConfig(key="visit", enabled=True, name="Visit", folder="01")
        mobiwork = FakeMobiWork(daily_records=[])
        manifest = sync_main._new_manifest("incremental", True)
        with tempfile.TemporaryDirectory() as temp_dir:
            export_path = Path(temp_dir) / "Visit.xlsx"
            export_path.write_bytes(b"x")
            with (
                patch.object(sync_main, "target_dates", return_value=[date(2026, 8, 21)]),
                patch.object(sync_main, "export_excel", return_value=export_path),
            ):
                sync_main.run_incremental(
                    [cfg], mobiwork, None, None, 1, True, manifest
                )
        self.assertIsNone(manifest["files"][0]["web_url"])


class BootstrapTests(unittest.TestCase):
    def test_empty_bootstrap_partition_stops_and_records_progress(self):
        cfg = ReportConfig(key="visit", enabled=True, name="Visit", folder="01")
        mobiwork = FakeMobiWork(range_records=[])
        manifest = sync_main._new_manifest("bootstrap", True)
        yesterday = datetime.now(sync_main.VN_TZ).date() - timedelta(days=1)

        sync_main.run_bootstrap(
            [cfg],
            mobiwork,
            None,
            None,
            True,
            1,
            yesterday,
            False,
            manifest,
        )

        self.assertEqual(manifest["bootstrap"]["partitions_processed"], 1)
        self.assertEqual(len(mobiwork.range_calls), 1)

    def test_completed_checkpoint_skips_rescan(self):
        cfg = ReportConfig(key="visit", enabled=True, name="Visit", folder="01")
        signature = sync_main._bootstrap_signature([cfg], date(2020, 1, 1), 24)
        sharepoint = FakeSharePoint(
            state={
                "signature": signature,
                "completed": True,
                "next_cursor_end": "2019-12-31",
                "consecutive_empty_months": 24,
            }
        )
        manifest = sync_main._new_manifest("bootstrap", False)

        sync_main.run_bootstrap(
            [cfg],
            FakeMobiWork(),
            sharepoint,
            "drive",
            False,
            24,
            date(2020, 1, 1),
            False,
            manifest,
        )

        self.assertEqual(manifest["bootstrap"]["checkpoint"], "already_complete")


class RunOrchestrationTests(unittest.TestCase):
    def test_run_incremental_success_writes_and_uploads_manifest(self):
        cfg = ReportConfig(key="visit", enabled=True, name="Visit", folder="01")
        sharepoint = FakeSharePoint()
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                with (
                    patch.object(sync_main, "enabled_reports", return_value=[cfg]),
                    patch.object(
                        sync_main,
                        "build_clients",
                        return_value=(FakeMobiWork(), sharepoint, "drive"),
                    ),
                    patch.object(sync_main, "run_incremental"),
                ):
                    result = sync_main.run(
                        "incremental", 3, False, 24, "2000-01-01", False
                    )
                saved = json.loads(
                    Path("output/sync_manifest.json").read_text(encoding="utf-8")
                )
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(result["status"], "success")
        self.assertEqual(saved["file_count"], 0)
        self.assertTrue(any("_sync_runs/" in item[1] for item in sharepoint.uploaded_json))

    def test_run_failure_writes_failure_manifest(self):
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                with patch.object(
                    sync_main,
                    "enabled_reports",
                    side_effect=RuntimeError("broken config"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "broken config"):
                        sync_main.run(
                            "incremental", 3, True, 24, "2000-01-01", False
                        )
                saved = json.loads(
                    Path("output/sync_manifest.json").read_text(encoding="utf-8")
                )
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(saved["status"], "failed")
        self.assertIn("broken config", saved["error"])

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            sync_main.run("invalid", 1, True, 24, "2000-01-01", False)


if __name__ == "__main__":
    unittest.main()
