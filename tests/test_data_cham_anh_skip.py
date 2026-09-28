import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import run_data_cham_anh as runner  # noqa: E402


class DataChamAnhSkipWhenUnchangedTests(unittest.TestCase):
    def _manifest(self, payload) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "sync_manifest.json"
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text, encoding="utf-8")
        return path

    def test_unchanged_only_for_successful_zero_write_sync(self):
        path = self._manifest({"status": "success", "dry_run": False, "sharepoint_write_count": 0})
        self.assertTrue(runner.report_sync_left_masters_unchanged(path))

    def test_any_write_failure_or_bad_manifest_forces_rebuild(self):
        cases = [
            {"status": "success", "dry_run": False, "sharepoint_write_count": 1},
            {"status": "partial_failure", "sharepoint_write_count": 0},
            {"status": "success", "dry_run": True, "sharepoint_write_count": 0},
            {"status": "success"},
            "not json",
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertFalse(runner.report_sync_left_masters_unchanged(self._manifest(payload)))
        self.assertFalse(runner.report_sync_left_masters_unchanged(Path("/nonexistent/manifest.json")))

    def test_run_skips_publish_only_when_flag_enabled_and_unchanged(self):
        env = {"DRY_RUN": "false", "DATA_CHAM_ANH_SKIP_WHEN_UNCHANGED": "true"}
        with patch.dict(os.environ, env, clear=False), \
                patch.object(runner, "report_sync_left_masters_unchanged", return_value=True), \
                patch.object(runner, "publish_target_months") as publish, \
                patch.object(runner.SemanticSharePointClient, "from_env") as client:
            self.assertEqual(runner.run(), [])
        publish.assert_not_called()
        client.assert_not_called()

    def test_run_publishes_when_flag_disabled_even_if_unchanged(self):
        env = {
            "DRY_RUN": "false",
            "DATA_CHAM_ANH_SKIP_WHEN_UNCHANGED": "false",
            "SHAREPOINT_DRIVE_ID": "drive",
            "SYNC_SCOPE": "today",
        }
        with patch.dict(os.environ, env, clear=False), \
                patch.object(runner, "report_sync_left_masters_unchanged", return_value=True), \
                patch.object(runner.core, "enabled_reports", return_value=[]), \
                patch.object(runner, "publish_target_months", return_value=[{"month": "x"}]) as publish, \
                patch.object(runner.SemanticSharePointClient, "from_env"):
            self.assertEqual(runner.run(), [{"month": "x"}])
        publish.assert_called_once()


if __name__ == "__main__":
    unittest.main()
