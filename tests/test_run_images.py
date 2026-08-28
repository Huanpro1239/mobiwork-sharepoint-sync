from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mobiwork import MobiWorkClient, ReportConfig
from run_images import DailyRangeMobiWorkClient


class DailyRangeMobiWorkClientTests(unittest.TestCase):
    def test_fetch_report_range_splits_into_daily_calls(self):
        calls = []

        def fake_fetch(base_self, cfg, from_date, to_date):
            calls.append((from_date, to_date))
            return [{"date": from_date.isoformat()}]

        client = DailyRangeMobiWorkClient(
            user="user",
            token="token",
            min_interval_seconds=0,
        )
        cfg = ReportConfig(
            key="visit",
            enabled=True,
            name="BaoCaoViengTham",
            folder="01_BaoCaoViengTham",
            url="https://example.invalid/visit",
        )

        with patch.object(MobiWorkClient, "fetch_report_range", new=fake_fetch):
            records = client.fetch_report_range(
                cfg,
                date(2026, 8, 26),
                date(2026, 8, 28),
            )

        self.assertEqual(
            calls,
            [
                (date(2026, 8, 26), date(2026, 8, 26)),
                (date(2026, 8, 27), date(2026, 8, 27)),
                (date(2026, 8, 28), date(2026, 8, 28)),
            ],
        )
        self.assertEqual(len(records), 3)

    def test_fetch_report_range_rejects_reverse_range(self):
        client = DailyRangeMobiWorkClient(
            user="user",
            token="token",
            min_interval_seconds=0,
        )
        cfg = ReportConfig(
            key="visit",
            enabled=True,
            name="BaoCaoViengTham",
            folder="01_BaoCaoViengTham",
            url="https://example.invalid/visit",
        )

        with self.assertRaises(ValueError):
            client.fetch_report_range(
                cfg,
                date(2026, 8, 28),
                date(2026, 8, 27),
            )


if __name__ == "__main__":
    unittest.main()
