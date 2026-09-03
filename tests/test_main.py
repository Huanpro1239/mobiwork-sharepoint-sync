import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import main as sync_main  # noqa: E402
from mobiwork import ReportConfig  # noqa: E402


class FakeSharePoint:
    def __init__(self):
        self.uploaded_json = []

    def upload_json(self, drive_id, remote_path, payload):
        self.uploaded_json.append((drive_id, remote_path, dict(payload)))
        return {"size": len(json.dumps(payload))}


class MainHelperTests(unittest.TestCase):
    def test_load_reports(self):
        payload = {
            "reports": [
                {"key": "a", "enabled": True, "name": "A", "folder": "A"},
                {"key": "b", "enabled": False, "name": "B", "folder": "B"},
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

    def test_bootstrap_gate_only_applies_to_real_github_production_with_drive(self):
        cases = [
            ({}, False),
            ({"GITHUB_ACTIONS": "true"}, False),
            ({"GITHUB_ACTIONS": "true", "SHAREPOINT_DRIVE_ID": "drive"}, True),
            (
                {
                    "GITHUB_ACTIONS": "true",
                    "SHAREPOINT_DRIVE_ID": "drive",
                    "DRY_RUN": "true",
                },
                False,
            ),
            (
                {
                    "GITHUB_ACTIONS": "true",
                    "SHAREPOINT_DRIVE_ID": "drive",
                    "BOOTSTRAP_BYPASS_GATE": "true",
                },
                False,
            ),
        ]
        controlled = {
            "GITHUB_ACTIONS",
            "SHAREPOINT_DRIVE_ID",
            "DRY_RUN",
            "BOOTSTRAP_BYPASS_GATE",
        }
        original = {key: os.environ.get(key) for key in controlled}
        try:
            for values, expected in cases:
                for key in controlled:
                    os.environ.pop(key, None)
                os.environ.update(values)
                self.assertEqual(sync_main._bootstrap_gate_required(), expected)
        finally:
            for key in controlled:
                os.environ.pop(key, None)
                if original[key] is not None:
                    os.environ[key] = original[key]

    def test_verify_bootstrap_ready_uses_sharepoint_state(self):
        state = {
            "status": "complete",
            "bootstrap_complete": True,
            "start_month": "2026-06",
            "end_month": "2026-09",
            "month_count_completed": 4,
            "month_count_expected": 4,
        }
        storage = object()
        env = {
            "GITHUB_ACTIONS": "true",
            "SHAREPOINT_DRIVE_ID": "drive",
            "DRY_RUN": "false",
            "BOOTSTRAP_BYPASS_GATE": "false",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(sync_main.SharePointClient, "from_env", return_value=storage),
            patch.object(sync_main, "require_bootstrap_ready", return_value=state) as gate,
        ):
            result = sync_main._verify_bootstrap_ready()

        self.assertEqual(result, state)
        gate.assert_called_once_with(storage, "drive")

    def test_enabled_reports_checks_bootstrap_gate_first(self):
        with patch.object(sync_main, "_verify_bootstrap_ready") as gate:
            reports = sync_main.enabled_reports()
        gate.assert_called_once_with()
        self.assertTrue(reports)

    def test_manifest_helpers_write_hash_and_upload_audit(self):
        cfg = ReportConfig(key="bill", enabled=True, name="Bill", folder="04")
        manifest = sync_main._new_manifest("incremental", False)
        sharepoint = FakeSharePoint()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Bill.xlsx"
            path.write_bytes(b"excel-bytes")
            sync_main._record_export(manifest, cfg, path, 7, "04/2026/08", None)
            sync_main._upload_manifest(manifest, sharepoint, "drive")

        self.assertEqual(manifest["files"][0]["source_rows"], 7)
        self.assertEqual(len(manifest["files"][0]["sha256"]), 64)
        self.assertEqual(len(sharepoint.uploaded_json), 1)
        self.assertIn("_sync_runs/", sharepoint.uploaded_json[0][1])

    def test_write_manifest_round_trip(self):
        manifest = sync_main._new_manifest("incremental", True)
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                path = sync_main._write_manifest(manifest)
                saved = json.loads(path.read_text(encoding="utf-8"))
            finally:
                os.chdir(previous)
        self.assertEqual(saved["run_id"], manifest["run_id"])
        self.assertEqual(saved["mode"], "incremental")


if __name__ == "__main__":
    unittest.main()
