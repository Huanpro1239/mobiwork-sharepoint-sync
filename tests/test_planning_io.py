from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

import openpyxl

from src.planning.config import PlanningConfig
from src.planning.source_refresh import (
    find_column_by_header,
    first_sheet_name,
    material_stock_last,
    sales_actual_cases,
    sheet_name_by_index,
)


def workbook_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Tong hop"
    ws1["B3"] = "Mã sản phẩm"
    ws1["C3"] = "Tên sản phẩm"
    ws2 = wb.create_sheet("Ton NVL")
    ws2.append(["x", "Mã", "x", "x", "x", "x", "x", "Tồn"])
    ws2.append([None, 130100001, None, None, None, None, None, 25])
    stream = BytesIO()
    wb.save(stream)
    return stream.getvalue()


class PlanningConfigTests(unittest.TestCase):
    def test_load_config_and_defaults(self):
        payload = {
            "planning_master_path": "folder/master.xlsm",
            "sources": {
                "ton": {
                    "path": "folder/ton.xlsx",
                    "sheet": "Data",
                    "start_row": 5,
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "planning.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            config = PlanningConfig.load(path)

        self.assertEqual(config.planning_master_path, "folder/master.xlsm")
        self.assertEqual(config.shadow_output_folder, "_PlanningEngine/shadow")
        self.assertEqual(config.sources["ton"].path, "folder/ton.xlsx")
        self.assertEqual(config.sources["ton"].sheet, "Data")
        self.assertEqual(config.sources["ton"].start_row, 5)


class PlanningSourceRefreshTests(unittest.TestCase):
    def test_workbook_sheet_helpers_and_header_lookup(self):
        data = workbook_bytes()
        self.assertEqual(first_sheet_name(data), "Tong hop")
        self.assertEqual(sheet_name_by_index(data, 2), "Ton NVL")
        self.assertEqual(find_column_by_header(data, "Tong hop", "ma san pham"), "B")

        with self.assertRaises(IndexError):
            sheet_name_by_index(data, 3)
        with self.assertRaises(KeyError):
            find_column_by_header(data, "Missing", "ma san pham")
        with self.assertRaises(KeyError):
            find_column_by_header(data, "Tong hop", "không tồn tại")

    def test_material_stock_last_matches_vba_semantics(self):
        rows = [
            {"B": "130100001", "H": 10},
            {"B": "not-a-code", "H": 999},
            {"B": "130100001", "H": 25},
            {"B": "130100002", "H": "7"},
        ]
        result = material_stock_last(rows, ["130100001", "130100002", "ABC"])
        self.assertEqual(result["130100001"], 25)
        self.assertEqual(result["130100002"], 7)
        self.assertIsNone(result["ABC"])

    def test_sales_actual_cases_filters_and_converts(self):
        source1 = [
            {"A": "KA", "O": "1301", "Q": 120},
            {"A": "MT", "O": "1301", "Q": 60},
        ]
        source2 = [
            {"A": "GT", "O": "1301", "Q": 24, "LoaiHoaDon": "Hoa don ban", "K": "VKD1"},
            {"A": "GT", "O": "1301", "Q": 999, "LoaiHoaDon": "Hoa don ban", "K": "VKD3"},
            {"A": "GT", "O": "1301", "Q": 999, "LoaiHoaDon": "Khac", "K": "VKD1"},
        ]
        rows = sales_actual_cases(
            source1,
            source2,
            ["1301"],
            ["KA/MT", "GT"],
            {"1301": 12},
        )
        self.assertEqual(rows[0]["KA/MT"], 15)
        self.assertEqual(rows[0]["GT"], 2)


if __name__ == "__main__":
    unittest.main()
