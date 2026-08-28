from __future__ import annotations

import unittest
from datetime import date

from src.planning.domain.production import build_algorithmic_daily_schedule


class PlanningDailyScheduleTests(unittest.TestCase):
    def test_out_of_month_start_is_reported_as_unscheduled(self):
        weekly = [
            {
                "Source row": 22,
                "Ma SP": "130300005",
                "Ten SP": "Sport Drink",
                "DVT": "Thùng",
                "Chuyen": "KHS",
                "So luong/ca": 4000,
                "So ca/ngay": 3,
                "SL SX tron me/ca": 3230,
                "Ngay bat dau SX": date(2026, 10, 5),
                "Ton dau thuc te": 7056,
            }
        ]
        rows = build_algorithmic_daily_schedule(
            weekly, plan_year=2026, plan_month=8
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["SL da xep"], 0)
        self.assertEqual(rows[0]["SL chua xep"], 3230)
        self.assertFalse(any(key.startswith("2026-08-") for key in rows[0]))

    def test_in_month_schedule_conserves_quantity(self):
        weekly = [
            {
                "Source row": 4,
                "Ma SP": "P1",
                "Ten SP": "Product 1",
                "DVT": "Thùng",
                "Chuyen": "KHS",
                "So luong/ca": 3000,
                "So ca/ngay": 2,
                "SL SX tron me/ca": 6000,
                "Ngay bat dau SX": date(2026, 8, 1),
                "Ton dau thuc te": 0,
            }
        ]
        rows = build_algorithmic_daily_schedule(
            weekly, plan_year=2026, plan_month=8
        )
        self.assertEqual(rows[0]["SL da xep"], 6000)
        self.assertEqual(rows[0]["SL chua xep"], 0)
        scheduled = sum(
            value
            for key, value in rows[0].items()
            if key.startswith("2026-08-")
        )
        self.assertEqual(scheduled, 6000)


if __name__ == "__main__":
    unittest.main()
