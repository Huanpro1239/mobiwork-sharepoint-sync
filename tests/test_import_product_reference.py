from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook

from import_product_reference import REQUIRED, code, read_reference, update_config


class ProductReferenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.source = Path(self.folder.name) / "catalogue.xlsx"

    def write(self, rows, headers=None):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "DanhMucSanPham"
        sheet.append(headers or [*REQUIRED, "Brand 1"])
        for row in rows:
            sheet.append(row)
        workbook.save(self.source)
        workbook.close()

    def test_aliases_and_pack_sizes(self):
        self.write([[100, 200, 24, "Thùng", "Brand", "1Way", "Lon"],
                    [101, 201, 20, "Két", "Brand", "2Way", None],
                    [102, 202, 12, "Thùng", "Brand", "1Way", None],
                    [None] * 7])
        products, units = read_reference(self.source)
        self.assertEqual(len(products), 6)
        self.assertEqual(products["100"], products["200"])
        self.assertEqual(units["100|Lon"]["units_per_pack"], "24")
        self.assertEqual(units["201|Chai"]["target_unit"], "Két")
        self.assertEqual(Decimal(units["202|Chai"]["factor"]), Decimal(1) / 12)

    def test_semi_finished_has_metadata_without_invented_factor(self):
        self.write([[100, 200, None, None, "Semi Product", "1Way", None]])
        products, units = read_reference(self.source)
        self.assertEqual(len(products), 2)
        self.assertEqual(units, {})

    def test_invalid_reference_rejected(self):
        for size, unit, brand in [(0, "Thùng", "B"), (-1, "Thùng", "B"),
                                  (1.5, "Thùng", "B"), ("NaN", "Thùng", "B"),
                                  ("bad", "Thùng", "B"), (24, "Chai", "B"),
                                  (24, "Thùng", None)]:
            with self.subTest(size=size, unit=unit, brand=brand):
                self.write([[100, 200, size, unit, brand, "1Way", None]])
                with self.assertRaises(ValueError):
                    read_reference(self.source)
        self.write([], headers=["wrong"])
        with self.assertRaises(ValueError):
            read_reference(self.source)

    def test_conflicting_aliases_rejected(self):
        original = [100, 200, 24, "Thùng", "B", "1Way", None]
        for conflicting in [[100, 201, 12, "Thùng", "B", "1Way", None],
                            [100, 201, 24, "Thùng", "Other", "1Way", None]]:
            self.write([original, conflicting])
            with self.assertRaises(ValueError):
                read_reference(self.source)
        self.write([original, original])
        self.assertEqual(len(read_reference(self.source)[0]), 2)

    def test_import_replaces_stale_factors_and_preserves_other_dimensions(self):
        self.write([[100, 200, None, None, "Semi Product", "1Way", None]])
        destination = Path(self.folder.name) / "config.json"
        destination.write_text(json.dumps({"publish_enabled": False, "employees": {"NV": {}},
                                          "unit_conversions": {"100|Chai": {"factor": 1},
                                                               "OTHER|Chai": {"factor": 2}}}))
        result = update_config(self.source, destination)
        cfg = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(result, {"product_codes": 2, "unit_conversions": 0})
        self.assertEqual(cfg["unit_conversions"], {"OTHER|Chai": {"factor": 2}})
        self.assertEqual(cfg["employees"], {"NV": {}})
        self.assertFalse(cfg["publish_enabled"])
        self.assertEqual(cfg["product_reference"]["codes_without_pack"], ["100", "200"])
        self.assertEqual(len(cfg["product_reference"]["sha256"]), 64)
        before = destination.read_bytes()
        self.write([[100, 200, -1, "Thùng", "B", "1Way", None]])
        with self.assertRaises(ValueError):
            update_config(self.source, destination)
        self.assertEqual(destination.read_bytes(), before)

    def test_codes_remain_text(self):
        self.assertEqual(code("00100"), "00100")
        self.assertEqual(code(100.0), "100")
        self.assertEqual(code(None), "")
        for value in [True, 1.5]:
            with self.assertRaises(ValueError):
                code(value)
