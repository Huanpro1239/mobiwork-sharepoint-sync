import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import bootstrap_history as bootstrap  # noqa: E402


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 3, 10, 0, tzinfo=tz)


class FakeSharePoint:
    def __init__(self):
        self.states = []

    def upload_json(self, drive_id, remote_path, payload):
        self.states.append((drive_id, remote_path, payload))
        return {"id": "state"}


class BootstrapMonthRangeTests(unittest.TestCase):
    def test_default_range_runs_oldest_to_current_month(self):
        with patch.object(bootstrap, "datetime", FixedDateTime):
            anchors = bootstrap.resolve_month_anchors("2026-06", "")

        self.assertEqual(
            anchors,
            [
                date(2026, 6, 30),
                date(2026, 7, 31),
                date(2026, 8, 31),
                date(2026, 9, 3),
            ],
        )

    def test_explicit_past_end_month_uses_calendar_month_end(self):
        with patch.object(bootstrap, "datetime", FixedDateTime):
            anchors = bootstrap.resolve_month_anchors("2026-06", "2026-07")
        self.assertEqual(anchors, [date(2026, 6, 30), date(2026, 7, 31)])

    def test_future_month_is_rejected(self):
        with (
            patch.object(bootstrap, "datetime", FixedDateTime),
            self.assertRaisesRegex(ValueError, "future"),
        ):
            bootstrap.resolve_month_anchors("2026-06", "2026-10")

    def test_end_before_start_is_rejected(self):
        with (
            patch.object(bootstrap, "datetime", FixedDateTime),
            self.assertRaisesRegex(ValueError, "after START_MONTH"),
        ):
            bootstrap.resolve_month_anchors("2026-08", "2026-07")


class BootstrapExecutionTests(unittest.TestCase):
    def test_failure_stops_before_later_months(self):
        reports = [SimpleNamespace(key="visit"), SimpleNamespace(key="bill")]
        anchors = [date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 31)]
        calls = []
        sharepoint = FakeSharePoint()

        def fake_rebuild(reports_arg, anchor, mobiwork, sharepoint, drive_id, dry_run, manifest):
            calls.append(anchor)
            if anchor.month == 7:
                return [
                    {"report": "visit", "target_date": anchor.isoformat(), "status": "success"},
                    {"report": "bill", "target_date": anchor.isoformat(), "status": "failed"},
                ]
            return [
                {"report": report.key, "target_date": anchor.isoformat(), "status": "success"}
                for report in reports_arg
            ]

        manifest = {}
        with patch.object(bootstrap.rebuild_month, "run_rebuild_set", side_effect=fake_rebuild):
            results, completed = bootstrap.run_bootstrap_set(
                reports,
                anchors,
                object(),
                sharepoint,
                "drive",
                False,
                manifest,
            )

        self.assertEqual(calls, [date(2026, 6, 30), date(2026, 7, 31)])
        self.assertEqual(completed, ["2026-06"])
        self.assertEqual(manifest["failed_month"], "2026-07")
        self.assertEqual(len(results), 4)
        self.assertEqual(len(sharepoint.states), 1)
        self.assertEqual(sharepoint.states[0][2]["status"], "running")
        self.assertEqual(sharepoint.states[0][2]["months_completed"], ["2026-06"])

    def test_all_months_complete_in_order(self):
        reports = [SimpleNamespace(key="visit")]
        anchors = [date(2026, 6, 30), date(2026, 7, 31)]
        calls = []
        sharepoint = FakeSharePoint()

        def fake_rebuild(reports_arg, anchor, mobiwork, sharepoint, drive_id, dry_run, manifest):
            calls.append(anchor)
            return [{"report": "visit", "target_date": anchor.isoformat(), "status": "success"}]

        with patch.object(bootstrap.rebuild_month, "run_rebuild_set", side_effect=fake_rebuild):
            _, completed = bootstrap.run_bootstrap_set(
                reports,
                anchors,
                object(),
                sharepoint,
                "drive",
                False,
                {},
            )

        self.assertEqual(calls, anchors)
        self.assertEqual(completed, ["2026-06", "2026-07"])
        self.assertEqual(len(sharepoint.states), 2)
        self.assertEqual(sharepoint.states[-1][2]["months_completed"], ["2026-06", "2026-07"])


if __name__ == "__main__":
    unittest.main()
