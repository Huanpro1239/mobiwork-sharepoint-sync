import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

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
                import os

                os.chdir(temp_dir)
                path = sync_main._write_manifest(manifest)
                saved = json.loads(path.read_text(encoding="utf-8"))
            finally:
                os.chdir(previous)
        self.assertEqual(saved["run_id"], manifest["run_id"])
        self.assertEqual(saved["mode"], "incremental")


if __name__ == "__main__":
    unittest.main()
