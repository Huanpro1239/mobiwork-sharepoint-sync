from __future__ import annotations

import calendar
import unittest
from datetime import date

from src.planning.formula_port import (
    build_abc_rows,
    build_algorithmic_daily_schedule,
    build_daily_material_allocation,
    build_finished_goods_projection,
    build_weekly_production_plan,
    material_demand_periods,
    purchase_dates,
    purchase_quantity,
    shortage_date,
)


class FormulaPortTests(unittest.TestCase):
    def test_finished_goods_projection_matches_tinh_ung_hang_semantics(self):
        rows = build_finished_goods_projection(
            ["P1"],
            stock_vikoda={"P1": 10},
            stock_vkd={"P1": 5},
            plant_stock={"P1": 3},
            actual_sales={"P1": 4},
            forecast_current={"P1": 20},
            forecast_m1={"P1": 30},
            forecast_m2={"P1": 40},
            forecast_m3={"P1": 50},
            warehouse_debt={"P1": 2},
        )
        row = rows[0]
        self.assertEqual(row["G Ton cac kho khac"], 12)
        self.assertEqual(row["J Con lai"], 13)
        self.assertEqual(row["L Du kien vat tu"], 15)
        self.assertEqual(row["M FC M+1"], 30)

    def test_flat_bom_material_demand_uses_positive_remaining_only(self):
        projections = [
            {
                "Ma SP": "P1",
                "J Con lai": -5,
                "M FC M+1": 10,
                "N FC M+2": 20,
                "O FC M+3": 30,
            },
            {
                "Ma SP": "P2",
                "J Con lai": 4,
                "M FC M+1": 5,
                "N FC M+2": 0,
                "O FC M+3": 1,
            },
        ]
        flat = [
            {"product_code": "P1", "material_code": "M", "qty_per_product": 2},
            {"product_code": "P2", "material_code": "M", "qty_per_product": 3},
        ]
        out = material_demand_periods(projections, flat)["M"]
        self.assertEqual(out["E"], 12)
        self.assertEqual(out["F"], 35)
        self.assertEqual(out["G"], 40)
        self.assertEqual(out["H"], 63)

    def test_shortage_date_uses_excel_proportional_current_month_logic(self):
        result = shortage_date(
            today=date(2026, 8, 28),
            stock=10,
            open_po=0,
            debt=0,
            demand_current=20,
            demand_m1=0,
            demand_m2=0,
            demand_m3=0,
        )
        self.assertEqual(result, date(2026, 8, 30))

    def test_abc_tie_semantics_match_sumif_greater_plus_current(self):
        material = [
            {"A": "M1", "B": "1", "D": 0, "E": 5, "F": "Nguyên liệu"},
            {"A": "M2", "B": "2", "D": 0, "E": 5, "F": "Nguyên liệu"},
            {"A": "M3", "B": "3", "D": 0, "E": 5, "F": "Nguyên liệu"},
        ]
        demand = {
            "M1": {"F": 80, "G": 0, "H": 0},
            "M2": {"F": 10, "G": 0, "H": 0},
            "M3": {"F": 10, "G": 0, "H": 0},
        }
        rows = build_abc_rows(
            material,
            demand_periods=demand,
            stock={},
            open_po={},
            debt={},
        )
        by_code = {row["Ma NVL"]: row for row in rows}
        self.assertEqual(by_code["M1"]["I ABC"], "A")
        self.assertEqual(by_code["M2"]["I ABC"], "B")
        self.assertEqual(by_code["M3"]["I ABC"], "B")

    def test_purchase_quantity_respects_abc_risk_leadtime_and_moq(self):
        qty = purchase_quantity(
            abc="A",
            risk="🟢 Thấp",
            leadtime=20,
            moq=100,
            available=10,
            demand_current=20,
            demand_m1=100,
            demand_m2=100,
            demand_m3=100,
            days_to_shortage=10,
        )
        self.assertEqual(qty, 200)

    def test_purchase_dates_follow_previous_workday_and_batch_day(self):
        purchase, order = purchase_dates(
            today=date(2026, 8, 28),
            shortage=date(2026, 9, 14),
            leadtime=5,
            suggested_qty=100,
            holidays=set(),
        )
        self.assertEqual(purchase, date(2026, 9, 9))
        self.assertEqual(order, date(2026, 9, 7))

    def test_weekly_plan_preserves_excel_roundup_for_negative_need(self):
        rows = build_weekly_production_plan(
            [
                {
                    "A": "130100096",
                    "B": "Alkaline lon",
                    "C": "Thùng",
                    "D": 4500,
                    "E": 4500,
                    "F": "KHS",
                    "G": "Lon",
                    "H": "Không đường",
                    "J": 3,
                }
            ],
            plan_month=8,
            plan_year=2026,
            actual_stock={"130100096": 8444},
            opening_book_stock={"130100096": 13161.833333333334},
            forecast={"130100096": 5343.6},
            projected_end_stock={"130100096": 0},
            warehouse_debt={"130100096": 0},
            daily_sales={"130100096": 5343.6 / 26},
            leadtime={"130100096": 3},
        )
        row = rows[0]
        self.assertAlmostEqual(row["SL can san xuat"], -7818.233333333334)
        self.assertEqual(row["SL SX tron me/ca"], -9000)
        self.assertAlmostEqual(row["So ngay can SX"], -2 / 3)
        self.assertEqual(row["Ngay bat dau SX"], date(2026, 9, 8))

    def test_galon_19l_daily_schedule_skips_sundays_and_keeps_total(self):
        plan_month = 9
        weekly = [
            {
                "Source row": 16,
                "Ma SP": "130100006",
                "Ten SP": "19L",
                "DVT": "Bình",
                "Chuyen": "Galon",
                "Ton dau thuc te": 100,
                "So luong/ca": 100,
                "So ca/ngay": 2,
                "SL SX tron me/ca": 3000,
                "Ngay bat dau SX": date(2026, plan_month, 1),
            }
        ]
        rows = build_algorithmic_daily_schedule(
            weekly, plan_year=2026, plan_month=plan_month
        )
        row = rows[0]
        quantities = []
        for day in range(1, calendar.monthrange(2026, plan_month)[1] + 1):
            current = date(2026, plan_month, day)
            qty = row.get(current.isoformat(), 0)
            if current.weekday() == 6:
                self.assertEqual(qty, 0)
            quantities.append(qty)
        self.assertAlmostEqual(sum(quantities), 3000)

    def test_daily_material_shortage_accounts_for_po_delivery_date(self):
        daily = [
            {
                "Ma SP": "P1",
                "2026-09-01": 10,
                "2026-09-02": 10,
                "2026-09-03": 10,
            }
        ]
        flat = [{"product_code": "P1", "material_code": "M1", "qty_per_product": 2}]
        materials = [{"A": "M1", "B": "Material", "C": "kg"}]
        po = [
            {
                "material_code": "M1",
                "remaining": 30,
                "delivery_date": date(2026, 9, 3),
            }
        ]
        rows = build_daily_material_allocation(
            materials,
            flat_bom_rows=flat,
            daily_product_rows=daily,
            stock={"M1": 25},
            po_lines=po,
            start_date=date(2026, 9, 1),
            horizon_days=3,
        )
        row = rows[0]
        self.assertEqual(row["D Nhu cau tu ngay"], 60)
        self.assertEqual(row["G PO trong ky"], 30)
        self.assertEqual(row["H Ngay thieu dau tien"], date(2026, 9, 2))
        self.assertEqual(row["I Trang thai"], "Can dat mua them")
        self.assertEqual(row["J Can mua them"], 5)


if __name__ == "__main__":
    unittest.main()
