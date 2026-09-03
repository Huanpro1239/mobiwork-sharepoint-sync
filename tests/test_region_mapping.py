import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from region_mapping import employee_prefix, enrich_visit_records, load_region_map  # noqa: E402


class RegionMappingTests(unittest.TestCase):
    def test_employee_prefix_uses_leading_letters(self):
        self.assertEqual(employee_prefix("QUNI0101"), "QUNI")
        self.assertEqual(employee_prefix(" hcmc0601 "), "HCMC")
        self.assertIsNone(employee_prefix(""))

    def test_qn_employee_stays_mien_bac_even_for_ka_customer(self):
        rows = [
            {
                "ma_nv": "QUNI0101",
                "ten_nhan_vien": "Vũ Văn Tân",
                "loai_kh": "KA Miền Trung 1",
                "ma_kh": "TEST001",
            }
        ]
        enriched = enrich_visit_records(rows, strict=True)
        self.assertEqual(enriched[0]["vung_code"], "MB")
        self.assertEqual(enriched[0]["vung"], "Miền Bắc")
        self.assertEqual(enriched[0]["vung_source"], "ma_nv_prefix")
        self.assertEqual(enriched[0]["loai_kh"], "KA Miền Trung 1")

    def test_unknown_prefix_fails_in_strict_mode(self):
        with self.assertRaisesRegex(ValueError, "Unmapped employees/prefixes"):
            enrich_visit_records([{"ma_nv": "NEWX0101"}], strict=True)

    def test_unknown_prefix_is_explicit_when_strict_disabled(self):
        enriched = enrich_visit_records([{"ma_nv": "NEWX0101"}], strict=False)
        self.assertIsNone(enriched[0]["vung"])
        self.assertIsNone(enriched[0]["vung_code"])
        self.assertEqual(enriched[0]["vung_source"], "unmapped")

    def test_master_contains_current_operating_prefixes(self):
        mapping = load_region_map()
        expected = {
            "BAGI", "HANC", "HAPH", "HUYE", "NIBI", "QUNI", "VIPH",
            "BITH", "DALA", "DANO", "KHA", "KHHO", "PHYE",
            "DANA", "QUNA", "QUNG",
            "BDG", "BIDU", "BRVT", "HCM", "HCMC", "VTU",
        }
        self.assertTrue(expected.issubset(mapping))


if __name__ == "__main__":
    unittest.main()
