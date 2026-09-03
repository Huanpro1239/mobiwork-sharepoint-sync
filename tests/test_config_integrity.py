from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProductionConfigIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads((ROOT / "config" / "reports.json").read_text(encoding="utf-8"))
        cls.reports = {item["key"]: item for item in cls.payload["reports"]}

    def test_required_production_reports_are_enabled_and_unique(self):
        raw = self.payload["reports"]
        keys = [item["key"] for item in raw]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), {"visit", "new_customer", "order", "bill"})
        self.assertTrue(all(item.get("enabled") is True for item in raw))

    def test_business_keys_are_declared_for_customer_order_and_bill(self):
        expected = {
            "new_customer": ["makh"],
            "order": ["ma_phieu"],
            "bill": ["ma_phieu"],
        }
        for report_key, business_key in expected.items():
            report = self.reports[report_key]
            self.assertEqual(report.get("primary_key"), business_key)
            self.assertEqual(report.get("required_fields"), business_key)

    def test_order_and_bill_fetch_all_statuses_by_creation_date(self):
        for report_key in ("order", "bill"):
            fixed = self.reports[report_key]["fixed_params"]
            self.assertEqual(fixed.get("kieu_ngay"), "cdate")
            self.assertEqual(fixed.get("trang_thai"), "")

    def test_bill_has_api_total_completeness_check(self):
        self.assertEqual(self.reports["bill"].get("total_path"), "total")

    def test_visit_region_master_has_only_supported_region_codes(self):
        payload = json.loads(
            (ROOT / "config" / "employee_regions.json").read_text(encoding="utf-8")
        )
        regions = payload["regions"]
        self.assertGreater(len(regions), 0)
        supported = {"MB", "MT1", "MT2", "MN"}
        for prefix, mapping in regions.items():
            self.assertTrue(prefix.isalpha() and prefix == prefix.upper())
            self.assertIn(mapping["vung_code"], supported)
            self.assertTrue(str(mapping["vung"]).strip())


if __name__ == "__main__":
    unittest.main()
