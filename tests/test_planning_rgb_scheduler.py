from __future__ import annotations

import unittest
from datetime import date

from src.planning.rgb_scheduler import build_rgb_daily_schedule


class RGBSchedulerTests(unittest.TestCase):
    def test_rgb_runs_sequentially_and_preserves_weekly_quantity(self):
        weekly = [
            {
                "Source row": 19,
                "Ma SP": "130100008",
                "Ten SP": "RGB gas",
                "DVT": "Ket",
                "Chuyen": "RGB",
                "Nhom SP": "RGB co gas",
                "So luong/ca": 4500,
                "So ca/ngay": 2,
                "SL SX tron me/ca": 90000,
                "Ngay bat dau SX": date(2026, 8, 11),
            },
            {
                "Source row": 20,
                "Ma SP": "130100013",
                "Ten SP": "RGB lemon",
                "DVT": "Ket",
                "Chuyen": "RGB",
                "Nhom SP": "RGB co gas",
                "So luong/ca": 4500,
                "So ca/ngay": 2,
                "SL SX tron me/ca": 26000,
                "Ngay bat dau SX": date(2026, 8, 11),
            },
            {
                "Source row": 21,
                "Ma SP": "130100149",
                "Ten SP": "RGB no gas",
                "DVT": "Ket",
                "Chuyen": "RGB",
                "Nhom SP": "RGB khong gas",
                "So luong/ca": 1600,
                "So ca/ngay": 2,
                "SL SX tron me/ca": 32000,
                "Ngay bat dau SX": date(2026, 8, 2),
            },
        ]

        rows = build_rgb_daily_schedule(
            weekly,
            plan_year=2026,
            plan_month=8,
        )
        self.assertEqual(
            [row["Ma SP"] for row in rows],
            ["130100149", "130100008", "130100013"],
        )

        occupied_by_date: dict[str, int] = {}
        for row in rows:
            scheduled = sum(
                value
                for key, value in row.items()
                if key.startswith("2026-08-")
            )
            self.assertAlmostEqual(scheduled, row["SL ke hoach"])
            self.assertEqual(row["SL chua xep"], 0)
            for key, value in row.items():
                if key.startswith("2026-08-") and value > 0:
                    occupied_by_date[key] = occupied_by_date.get(key, 0) + 1

        self.assertTrue(all(count == 1 for count in occupied_by_date.values()))
        self.assertEqual(rows[0]["Ngay bat dau auto"], date(2026, 8, 2))
        self.assertEqual(rows[1]["Ngay bat dau auto"], date(2026, 8, 12))

    def test_rgb_reports_quantity_that_does_not_fit_in_month(self):
        rows = build_rgb_daily_schedule(
            [
                {
                    "Source row": 19,
                    "Ma SP": "P1",
                    "Chuyen": "RGB",
                    "So luong/ca": 100,
                    "So ca/ngay": 2,
                    "SL SX tron me/ca": 1000,
                    "Ngay bat dau SX": date(2026, 8, 30),
                }
            ],
            plan_year=2026,
            plan_month=8,
        )
        self.assertEqual(rows[0]["SL chua xep"], 600)


if __name__ == "__main__":
    unittest.main()
