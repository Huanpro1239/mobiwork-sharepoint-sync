from __future__ import annotations

import unittest

from src.planning.source_refresh import material_stock_last
from src.planning.vba_port import (
    aggregate_gui_kho,
    aggregate_sales_actual,
    aggregate_xuat_kho,
    build_divisor_map,
    explode_bom,
    map_gui_kho_to_products,
    nokho_balance,
    nokho_col_d,
    nokho_col_e,
    purchase_suggestions,
    sales_channels_in_cases,
    sum_two_divided_stocks,
)


class TestPlanningVbaPort(unittest.TestCase):
    def test_divisor_first_duplicate_wins(self):
        rows = [{"C": "1301", "F": 24}, {"C": "1301", "F": 12}]
        self.assertEqual(build_divisor_map(rows)["1301"], 24)

    def test_sales_actual_filters_and_ka_mt_merge(self):
        totals = aggregate_sales_actual(
            # BCBANHANG source 1 uses 2xxxxxxxx codes and converts them to
            # the 1xxxxxxxx destination code before aggregation.
            [{"A": "KA", "O": "2301", "Q": 24}],
            [
                {"A": "MT", "O": "1301", "Q": 12, "K": "C001", "LoaiHoaDon": "Hóa đơn bán"},
                {"A": "KA", "O": "1301", "Q": 999, "K": "VKD3", "LoaiHoaDon": "Hoa don ban"},
                {"A": "KA", "O": "1301", "Q": 999, "K": "C002", "LoaiHoaDon": "Tra hang"},
            ],
        )
        converted = sales_channels_in_cases("1301", ["KA/MT"], totals, 12)
        self.assertEqual(converted["KA/MT"], 3)
        self.assertNotIn(("2301", "KA"), totals)

    def test_material_stock_numeric_code_last_duplicate_wins(self):
        result = material_stock_last(
            [
                {"B": "00123", "H": 10},
                {"B": 123.0, "H": 15},
                {"B": "ABC", "H": 99},
            ],
            [123, 456],
        )
        self.assertEqual(result["123"], 15)
        self.assertIsNone(result["456"])

    def test_gui_kho_sum_and_code_map(self):
        source = aggregate_gui_kho([{"J": "2301", "AG": 10}, {"J": "2301", "AG": 5}])
        mapped = map_gui_kho_to_products(["1301"], source)
        self.assertEqual(mapped["1301"], 15)

    def test_nokho_duplicate_semantics_and_balance(self):
        d = nokho_col_d([{"B": "1301", "U": 10}, {"B": "1301", "U": 12}], ["1301"])
        e = nokho_col_e([{"C": "1301", "G": 3, "O": 2}], ["1301"])
        g = nokho_balance(d, e, {"1301": 1})
        self.assertEqual(d["1301"], 12)
        self.assertEqual(e["1301"], 5)
        self.assertEqual(g["1301"], 8)

    def test_sum_two_divided_stocks_ap_2to1(self):
        result = sum_two_divided_stocks(
            ["1301"],
            [{"B": "1301", "M": 240}],
            [{"B": "2301", "M": 120}],
            {"1301": 12},
            value_column="M",
        )
        self.assertEqual(result["1301"], 30)

    def test_xuat_kho_filter_and_conversion(self):
        out = aggregate_xuat_kho(
            [{"I": "51C", "R": "1301", "S": "A", "T": "Thung", "U": 10}],
            [{"I": "51D", "R": "1301", "S": "A", "T": "Thung", "U": 5}],
        )
        by_code = {row["MASANPHAM"]: row["TONG SO LUONG XUAT"] for row in out}
        self.assertEqual(by_code["1301"], 10)
        self.assertEqual(by_code["2301"], 5)

    def test_recursive_bom(self):
        rows = [
            {"parent_code": "P", "child_code": "S", "qty": 2},
            {"parent_code": "S", "child_code": "R", "qty": 3},
        ]
        out = explode_bom(rows, ["P"])
        self.assertEqual(out[0]["material_code"], "R")
        self.assertEqual(out[0]["qty_per_product"], 6)

    def test_bom_cycle_fails_closed(self):
        rows = [
            {"parent_code": "A", "child_code": "B", "qty": 1},
            {"parent_code": "B", "child_code": "A", "qty": 1},
        ]
        with self.assertRaises(ValueError):
            explode_bom(rows, ["A"])

    def test_purchase_moq_rounding(self):
        out = purchase_suggestions({"NVL1": 105}, {"NVL1": 10}, {"NVL1": 5}, {"NVL1": 25})
        self.assertEqual(out[0].net_requirement, 90)
        self.assertEqual(out[0].suggested_order, 100)


if __name__ == "__main__":
    unittest.main()
