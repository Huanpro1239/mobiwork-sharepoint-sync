import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import rebuild_month as rebuild  # noqa: E402
from mobiwork import ReportConfig  # noqa: E402


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 3, 10, 0, tzinfo=tz)


class ResolveAnchorTests(unittest.TestCase):
    def test_blank_target_uses_current_vietnam_date(self):
        with patch.object(rebuild, "datetime", FixedDateTime):
            self.assertEqual(rebuild.resolve_anchor(""), FixedDateTime(2026, 9, 3).date())

    def test_current_month_rebuilds_through_today_only(self):
        with patch.object(rebuild, "datetime", FixedDateTime):
            self.assertEqual(
                rebuild.resolve_anchor("2026-09"),
                FixedDateTime(2026, 9, 3).date(),
            )

    def test_past_month_rebuilds_through_month_end(self):
        with patch.object(rebuild, "datetime", FixedDateTime):
            self.assertEqual(
                rebuild.resolve_anchor("2026-08"),
                FixedDateTime(2026, 8, 31).date(),
            )

    def test_future_month_is_rejected(self):
        with (
            patch.object(rebuild, "datetime", FixedDateTime),
            self.assertRaisesRegex(ValueError, "future"),
        ):
            rebuild.resolve_anchor("2026-10")

    def test_invalid_month_format_is_rejected(self):
        with (
            patch.object(rebuild, "datetime", FixedDateTime),
            self.assertRaisesRegex(ValueError, "YYYY-MM"),
        ):
            rebuild.resolve_anchor("09/2026")


class FakeMobiWork:
    def __init__(self, fail_key: str | None = None):
        self.fail_key = fail_key

    def fetch_report(self, cfg, target_date):
        if cfg.key == self.fail_key:
            raise RuntimeError("simulated source failure")
        return [{"id": f"{cfg.key}-{target_date.isoformat()}"}]


class FakeSharePoint:
    def __init__(self):
        self.uploads = []

    def upload_file(self, drive_id, path, remote_folder):
        self.uploads.append((drive_id, path.name, remote_folder))
        return {
            "size": path.stat().st_size,
            "verification_mode": "xlsx_semantic",
            "semantic_match": True,
            "upload_skipped": False,
            "webUrl": f"https://example/{path.name}",
        }

    def list_folder_children(self, drive_id, remote_folder):
        return []

    def delete_path(self, drive_id, remote_path):
        return True


class FullMonthSourceGateTests(unittest.TestCase):
    @staticmethod
    def _reports():
        return [
            ReportConfig(key="a", enabled=True, name="A", folder="01"),
            ReportConfig(key="b", enabled=True, name="B", folder="02"),
        ]

    def test_source_failure_blocks_all_sharepoint_writes(self):
        manifest = rebuild.core._new_manifest("rebuild_month", False)
        sharepoint = FakeSharePoint()
        current = Path.cwd()

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                results = rebuild.run_rebuild_set(
                    self._reports(),
                    date(2026, 8, 2),
                    FakeMobiWork(fail_key="b"),
                    sharepoint,
                    "drive",
                    False,
                    manifest,
                )
            finally:
                os.chdir(current)

        self.assertEqual(sharepoint.uploads, [])
        self.assertEqual([item["status"] for item in results], ["failed", "failed"])
        self.assertTrue(all(item["source_gate_passed"] is False for item in results))
        self.assertIn("source completeness gate", results[0]["error"])
        self.assertIn("simulated source failure", results[1]["error"])

    def test_all_sources_must_prepare_before_publish_begins(self):
        manifest = rebuild.core._new_manifest("rebuild_month", False)
        sharepoint = FakeSharePoint()
        current = Path.cwd()

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                results = rebuild.run_rebuild_set(
                    self._reports(),
                    date(2026, 8, 2),
                    FakeMobiWork(),
                    sharepoint,
                    "drive",
                    False,
                    manifest,
                )
            finally:
                os.chdir(current)

        self.assertEqual(len(sharepoint.uploads), 2)
        self.assertEqual([item["status"] for item in results], ["success", "success"])
        self.assertTrue(all(item["source_gate_passed"] is True for item in results))
        self.assertEqual(len(manifest["files"]), 2)


if __name__ == "__main__":
    unittest.main()
