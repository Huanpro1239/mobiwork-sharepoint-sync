from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import openpyxl

from src.planning.excel_io import write_shadow_workbook


class PlanningExcelIOTests(unittest.TestCase):
    def test_write_shadow_workbook_preserves_keys_from_later_rows(self):
        rows = [
            {
                "Ma SP": "P1",
                "2026-08-29": 100,
            },
            {
                "Ma SP": "P2",
                "2026-08-01": 200,
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shadow.xlsx"
            write_shadow_workbook({"Schedule": rows}, path)

            wb = openpyxl.load_workbook(path, data_only=True)
            ws = wb["Schedule"]
            headers = [cell.value for cell in ws[1]]

            self.assertEqual(
                headers,
                ["Ma SP", "2026-08-29", "2026-08-01"],
            )
            self.assertEqual(ws.cell(2, 2).value, 100)
            self.assertIsNone(ws.cell(2, 3).value)
            self.assertIsNone(ws.cell(3, 2).value)
            self.assertEqual(ws.cell(3, 3).value, 200)
            wb.close()


if __name__ == "__main__":
    unittest.main()
