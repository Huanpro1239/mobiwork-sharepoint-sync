import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import reconcile_history as reconcile  # noqa: E402
from mobiwork import ReportConfig  # noqa: E402


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 4, 10, 0, tzinfo=tz)


class ResolveCompletedHistoryTests(unittest.TestCase):
    def test_default_range_ends_at_previous_completed_month(self):
        with patch.object(reconcile, "datetime", FixedDateTime):
            anchors = reconcile.resolve_completed_history_anchors("2026-06", "")

        self.assertEqual(
            anchors,
            [
                date(2026, 6, 30),
                date(2026, 7, 31),
                date(2026, 8, 31),
            ],
        )

    def test_explicit_completed_range_is_preserved(self):
        with patch.object(reconcile, "datetime", FixedDateTime):
            anchors = reconcile.resolve_completed_history_anchors(
                "2026-07",
                "2026-08",
            )

        self.assertEqual(anchors, [date(2026, 7, 31), date(2026, 8, 31)])


class HistoricalReconcileSetTests(unittest.TestCase):
    @staticmethod
    def _reports():
        return [
            ReportConfig(key="a", enabled=True, name="A", folder="01"),
            ReportConfig(key="b", enabled=True, name="B", folder="02"),
        ]

    def test_months_run_oldest_to_newest_and_stop_after_failure(self):
        anchors = [date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 31)]
        calls = []

        def fake_rebuild(
            reports,
            anchor,
            mobiwork,
            sharepoint,
            drive_id,
            dry_run,
            manifest,
        ):
            calls.append(anchor.strftime("%Y-%m"))
            status = "failed" if anchor.month == 7 else "success"
            return [
                {
                    "report": cfg.key,
                    "target_date": anchor.isoformat(),
                    "status": status,
                }
                for cfg in reports
            ]

        manifest = {}
        with patch.object(
            reconcile.rebuild_month,
            "run_rebuild_set",
            side_effect=fake_rebuild,
        ):
            results, completed = reconcile.run_reconcile_set(
                self._reports(),
                anchors,
                object(),
                object(),
                "drive",
                False,
                manifest,
            )

        self.assertEqual(calls, ["2026-06", "2026-07"])
        self.assertEqual(completed, ["2026-06"])
        self.assertEqual(manifest["failed_month"], "2026-07")
        self.assertEqual(len(results), 4)

    def test_successful_range_records_all_completed_months(self):
        anchors = [date(2026, 6, 30), date(2026, 7, 31)]

        def fake_rebuild(
            reports,
            anchor,
            mobiwork,
            sharepoint,
            drive_id,
            dry_run,
            manifest,
        ):
            return [
                {
                    "report": cfg.key,
                    "target_date": anchor.isoformat(),
                    "status": "success",
                }
                for cfg in reports
            ]

        with patch.object(
            reconcile.rebuild_month,
            "run_rebuild_set",
            side_effect=fake_rebuild,
        ):
            results, completed = reconcile.run_reconcile_set(
                self._reports(),
                anchors,
                object(),
                object(),
                "drive",
                False,
                {},
            )

        self.assertEqual(completed, ["2026-06", "2026-07"])
        self.assertEqual(len(results), 4)
        self.assertTrue(all(item["status"] == "success" for item in results))


if __name__ == "__main__":
    unittest.main()
