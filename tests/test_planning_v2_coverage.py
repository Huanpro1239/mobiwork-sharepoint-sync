from __future__ import annotations

import unittest
from datetime import date, datetime
from io import BytesIO

import openpyxl

from src.planning import engine
from src.planning.formula_port import (
    abc_cycle,
    abc_feasibility,
    abc_risk,
    aggregate_open_po,
    build_algorithmic_daily_schedule,
    build_daily_material_allocation,
    build_fc_end_stock,
    build_material_inbound_plan,
    build_purchase_plan,
    forecast_by_month,
    purchase_action,
    purchase_dates,
    purchase_priority,
    purchase_status,
    standardize_direct_bom,
    standardize_flat_bom,
)


class PlanningV2CoverageTests(unittest.TestCase):
    def test_engine_helpers_parse_plan_month_and_dates(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Ke hoach SX tuan"
        ws["B2"] = "Tháng 9"
        stream = BytesIO()
        wb.save(stream)
        content = stream.getvalue()

        self.assertEqual(engine._plan_month(content, 8), 9)
        self.assertEqual(engine._date_only(date(2026, 8, 28)), date(2026, 8, 28))
        self.assertEqual(
            engine._date_only(datetime(2026, 8, 28, 12, 0)), date(2026, 8, 28)
        )
        self.assertIsNone(engine._date_only("2026-08-28"))

    def test_engine_map_helpers(self):
        dmsp = [
            {"C": "100", "J": 5},
            {"C": "100", "J": 9},
            {"C": "200", "J": 3},
        ]
        self.assertEqual(engine._leadtime_map(dmsp), {"100": 5, "200": 3})

        totals = engine._sales_total_map(
            [
                {"Ma SP": "100", "KA": 2, "MT": 3},
                {"Ma SP": "200", "KA": None, "GT": 4},
            ]
        )
        self.assertEqual(totals, {"100": 5, "200": 4})

        divided = engine._divided_map(
            ["100"],
            [{"B": "100", "M": 120}],
            {"100": 12},
            "M",
            "none",
        )
        self.assertEqual(divided["100"], 10)

    def test_forecast_and_bom_standardizers(self):
        fc = [{"B": "P1", "E": 10, "F": 20}]
        self.assertEqual(forecast_by_month(fc, 1)["P1"], 10)
        self.assertEqual(forecast_by_month(fc, 2)["P1"], 20)
        with self.assertRaises(ValueError):
            forecast_by_month(fc, 13)

        flat = standardize_flat_bom(
            [
                {"A": "P1", "B": "M1", "C": "Material", "D": 2},
                {"A": "", "B": "M2", "D": 3},
            ]
        )
        self.assertEqual(flat[0]["qty_per_product"], 2)
        self.assertEqual(len(flat), 1)

        direct = standardize_direct_bom(
            [
                {"A": "P1", "B": "Product", "C": "M1", "D": "Material", "E": 3},
                {"A": "P2", "B": "Product2", "C": "", "E": 1},
            ]
        )
        self.assertEqual(direct[0]["qty"], 3)
        self.assertEqual(len(direct), 1)

    def test_open_po_and_material_inbound_plan(self):
        totals, lines = aggregate_open_po(
            [
                {
                    "Ma Hang": "M1",
                    "So Luong mua": 100,
                    "So Luong nhan": 40,
                    "Ngay Giao": date(2026, 9, 5),
                },
                {
                    "Ma Hang": "M1",
                    "So Luong mua": 50,
                    "So Luong nhan": 60,
                    "Ngay Giao": None,
                },
            ]
        )
        self.assertEqual(totals["M1"], 60)
        self.assertEqual(lines[0]["remaining"], 60)
        self.assertEqual(lines[1]["remaining"], 0)

        rows = build_material_inbound_plan(
            [{"A": "M1", "B": "Mat", "C": "kg"}],
            stock={"M1": 20},
            direct_run_need={"M1": 50},
            open_po=totals,
            material_debt={"M1": 5},
        )
        row = rows[0]
        self.assertEqual(row["F Con thieu"], 30)
        self.assertEqual(row["G Ton PO"], 60)
        self.assertEqual(row["I Con thieu no kho"], 15)

    def test_abc_branch_variants(self):
        self.assertEqual(abc_risk(0, "A", 5, "", 0), "⚪ Không NC")
        self.assertEqual(abc_risk(100, "A", 5, "", 150), "🔴 Cao - đang dư")
        self.assertEqual(abc_risk(100, "C", 35, "", 20), "🟡 TB - C/LT dài")
        self.assertEqual(abc_risk(100, "C", 5, "", 20), "🔴 Cao - C/nhạy hạn")
        self.assertEqual(abc_risk(100, "B", 5, "", 20), "🟡 Trung bình")
        self.assertEqual(abc_risk(100, "A", 5, "", 20), "🟢 Thấp")

        self.assertEqual(abc_feasibility(0, 0, 5), "⚪ Không có NC")
        self.assertEqual(abc_feasibility(100, 1, 5), "✅ Đủ cho 3 tháng")
        self.assertIn("Thiếu nhiều", abc_feasibility(100, -60, 20))
        self.assertEqual(abc_feasibility(100, -30, 5), "🟠 Thiếu vừa")
        self.assertEqual(abc_feasibility(100, -10, 5), "🟡 Thiếu nhẹ")

        self.assertEqual(abc_cycle(0, 0, "A", 5, "🟢 Thấp", 0), "-")
        self.assertEqual(
            abc_cycle(100, 0, "A", 5, "🟢 Thấp", 150), "1 tháng (đã dư)"
        )
        self.assertEqual(
            abc_cycle(100, -10, "A", 35, "🟡 Trung bình", 20),
            "2 tháng / chia nhịp",
        )
        self.assertEqual(
            abc_cycle(100, 10, "B", 15, "🟡 Trung bình", 110), "2 tháng"
        )

    def test_purchase_status_action_priority_and_zero_dates(self):
        today = date(2026, 8, 28)
        self.assertEqual(
            purchase_status(
                total_demand=0,
                debt=0,
                available=0,
                shortage=None,
                today=today,
                abc="A",
                open_po=0,
                demand_current=0,
                demand_m1=0,
            ),
            "⚪ KHÔNG NC",
        )
        self.assertEqual(
            purchase_status(
                total_demand=100,
                debt=0,
                available=300,
                shortage=None,
                today=today,
                abc="A",
                open_po=0,
                demand_current=50,
                demand_m1=50,
            ),
            "✅ ĐỦ DƯ",
        )
        urgent = purchase_status(
            total_demand=100,
            debt=0,
            available=0,
            shortage=date(2026, 9, 3),
            today=today,
            abc="A",
            open_po=0,
            demand_current=50,
            demand_m1=50,
        )
        self.assertEqual(urgent, "🔴 NGUY CẤP")
        self.assertEqual(purchase_priority(urgent), 1)
        self.assertIn("KHẨN", purchase_action(urgent, 0))
        self.assertEqual(
            purchase_dates(
                today=today,
                shortage=None,
                leadtime=7,
                suggested_qty=0,
                holidays=set(),
            ),
            (None, None),
        )

    def test_full_purchase_plan_covers_no_need_and_need_rows(self):
        material = [
            {"A": "M0", "B": "No need", "C": "kg", "D": 10, "E": 5},
            {"A": "M1", "B": "Need", "C": "kg", "D": 25, "E": 20},
        ]
        demand = {
            "M0": {"E": 0, "F": 0, "G": 0, "H": 0},
            "M1": {"E": 50, "F": 100, "G": 80, "H": 60},
        }
        abc = [
            {"Ma NVL": "M0", "I ABC": "C", "O Rui ro ton/HSD": "⚪ Không NC"},
            {"Ma NVL": "M1", "I ABC": "A", "O Rui ro ton/HSD": "🟢 Thấp"},
        ]
        rows = build_purchase_plan(
            material,
            demand_periods=demand,
            stock={"M0": 0, "M1": 10},
            open_po={"M0": 0, "M1": 0},
            debt={"M0": 0, "M1": 0},
            abc_rows=abc,
            today=date(2026, 8, 28),
            holidays={date(2026, 9, 2)},
        )
        by_code = {row["Ma NVL"]: row for row in rows}
        self.assertEqual(by_code["M0"]["O Muc rui ro"], "⚪ KHÔNG NC")
        self.assertEqual(by_code["M0"]["P Cover"], "-")
        self.assertGreater(by_code["M1"]["L SL de xuat mua"], 0)
        self.assertIsNotNone(by_code["M1"]["U Ngay mua hang"])

    def test_fc_end_stock_branches(self):
        daily, end = build_fc_end_stock(
            ["P1", "P2"],
            current_forecast={"P1": 260, "P2": 260},
            gui_kho_begin={"P1": 100, "P2": 0},
            leadtime={"P1": 3, "P2": 3},
            working_days=26,
        )
        self.assertEqual(daily["P1"], 10)
        self.assertEqual(end["P1"], 0)
        self.assertEqual(end["P2"], 50)

    def test_khs_and_other_galon_daily_schedule(self):
        rows = [
            {
                "Source row": 4,
                "Ma SP": "K1",
                "Ten SP": "KHS 1",
                "DVT": "Thùng",
                "Chuyen": "KHS",
                "Ton dau thuc te": 1,
                "So luong/ca": 100,
                "So ca/ngay": 2,
                "SL SX tron me/ca": 300,
                "Ngay bat dau SX": date(2026, 9, 1),
            },
            {
                "Source row": 5,
                "Ma SP": "K2",
                "Ten SP": "KHS 2",
                "DVT": "Thùng",
                "Chuyen": "KHS",
                "Ton dau thuc te": 2,
                "So luong/ca": 100,
                "So ca/ngay": 2,
                "SL SX tron me/ca": 200,
                "Ngay bat dau SX": date(2026, 9, 1),
            },
            {
                "Source row": 6,
                "Ma SP": "G1",
                "Ten SP": "Other galon",
                "DVT": "Bình",
                "Chuyen": "Galon",
                "Ton dau thuc te": 0,
                "So luong/ca": 50,
                "So ca/ngay": 2,
                "SL SX tron me/ca": 150,
                "Ngay bat dau SX": date(2026, 9, 2),
            },
        ]
        scheduled = build_algorithmic_daily_schedule(
            rows, plan_year=2026, plan_month=9
        )
        by_code = {row["Ma SP"]: row for row in scheduled}
        self.assertAlmostEqual(
            sum(v for k, v in by_code["K1"].items() if k.startswith("2026-")), 300
        )
        self.assertAlmostEqual(
            sum(v for k, v in by_code["K2"].items() if k.startswith("2026-")), 200
        )
        self.assertEqual(by_code["G1"].get("2026-09-01", 0), 0)
        self.assertGreater(by_code["G1"].get("2026-09-02", 0), 0)

    def test_daily_material_allocation_all_statuses(self):
        materials = [
            {"A": "A", "B": "A", "C": "kg"},
            {"A": "B", "B": "B", "C": "kg"},
            {"A": "C", "B": "C", "C": "kg"},
            {"A": "D", "B": "D", "C": "kg"},
        ]
        flat = [
            {"product_code": "P", "material_code": code, "qty_per_product": 1}
            for code in ("A", "B", "C", "D")
        ]
        daily = [{"Ma SP": "P", "2026-09-01": 50, "2026-09-02": 50}]
        po = [
            {"material_code": "B", "remaining": 100, "delivery_date": date(2026, 9, 2)},
            {"material_code": "C", "remaining": 100, "delivery_date": None},
        ]
        rows = build_daily_material_allocation(
            materials,
            flat_bom_rows=flat,
            daily_product_rows=daily,
            stock={"A": 150, "B": 20, "C": 20, "D": 20},
            po_lines=po,
            start_date=date(2026, 9, 1),
            horizon_days=2,
        )
        by_code = {row["Ma NVL"]: row for row in rows}
        self.assertEqual(by_code["A"]["I Trang thai"], "Du hang")
        self.assertEqual(by_code["B"]["I Trang thai"], "Cho PO ve trong ky")
        self.assertEqual(
            by_code["C"]["I Trang thai"], "PO co nhung can xem ngay giao"
        )
        self.assertEqual(by_code["D"]["I Trang thai"], "Can dat mua them")


if __name__ == "__main__":
    unittest.main()
