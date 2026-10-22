import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from src.excel_export import build_order_frames
from src.mobiwork import MobiWorkClient, ReportConfig
from src.order_units import catalogue_conversions, load_conversions, standardise_order_units

PRODUCTS = {
    "SP1": {"ma_sp": "SP1", "dvt_chan": "Thùng", "dvt_le": "Chai", "hsqd": 24},
    "SP2": {"ma_sp": "SP2", "dvt_chan": "Két", "dvt_le": "Chai", "hsqd": "20"},
    "SP3": {"ma_sp": "SP3", "dvt_chan": "Bình", "dvt_le": "Chai", "hsqd": 0},
}
CONVERSIONS = catalogue_conversions(PRODUCTS)


def order(*items):
    return [{"ma_phieu": "DH1", "san_pham": list(items)}]


class StandardiseOrderUnitsTests(unittest.TestCase):
    def test_chai_line_is_rewritten_into_pack_unit(self):
        records = standardise_order_units(order(
            {"stt": 1, "ma_sp": "SP1", "ten_dvt": "Chai", "ma_dvt": "Chai", "so_luong": "48",
             "don_gia": 10000, "thanh_tien": 480000},
            {"stt": 2, "ma_sp": "SP2", "ten_dvt": "chai", "so_luong": 30},
        ), CONVERSIONS)
        first, second = records[0]["san_pham"]
        self.assertEqual((first["ten_dvt"], first["ma_dvt"], first["so_luong"]), ("Thùng", "Thùng", 2.0))
        # Prices stay exactly as DMS sent them.
        self.assertEqual((first["don_gia"], first["thanh_tien"]), (10000, 480000))
        self.assertEqual((second["ten_dvt"], second["so_luong"]), ("Két", 1.5))
        self.assertNotIn("dvt_chuan", first)

    def test_pack_unit_and_missing_hsqd_lines_are_kept_as_sent(self):
        source = order(
            {"stt": 1, "ma_sp": "SP1", "ten_dvt": "Thùng", "so_luong": 3},
            {"stt": 2, "ma_sp": "SP3", "ten_dvt": "Chai", "so_luong": 6},
            {"stt": 3, "ma_sp": "SP9", "ma_dvt": "Chai", "so_luong": 6},
        )
        with self.assertLogs("mobiwork_sync", "WARNING") as logs:
            records = standardise_order_units(source, CONVERSIONS)
        self.assertEqual(records[0]["san_pham"], source[0]["san_pham"])
        self.assertIn("SP3|Chai, SP9|Chai", logs.output[0])

    def test_promotional_lines_are_standardised_and_exported(self):
        records = standardise_order_units([{"ma_phieu": "DH1",
            "san_pham": [{"stt": 1, "ma_sp": "SP1", "ten_dvt": "Chai", "so_luong": 12}],
            "san_pham_km": [{"stt": 2, "ma_sp": "SP2", "ten_dvt": "Chai", "so_luong": 10}]}], CONVERSIONS)
        _, detail = build_order_frames(records)
        self.assertEqual(detail["so_luong"].tolist(), [0.5, 0.5])
        self.assertEqual(detail["ten_dvt"].tolist(), ["Thùng", "Két"])

    def test_catalogue_hsqd_overrides_configured_conversion(self):
        configured = {("SP1", "chai"): {"target_unit": "Thùng", "hsqd": Decimal(30), "source": "config"},
                      ("SP7", "chai"): {"target_unit": "Két", "hsqd": Decimal(12), "source": "config"}}
        with patch("src.order_units.configured_conversions", return_value=configured),                 patch("src.order_units.fetch_product_catalogue", return_value=PRODUCTS):
            conversions = load_conversions(object())
        self.assertEqual(conversions[("SP1", "chai")]["hsqd"], 24)
        self.assertEqual(conversions[("SP7", "chai")]["hsqd"], 12)

    def test_client_loads_conversions_once_for_flagged_report(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        client = MobiWorkClient("u", "t", min_interval_seconds=0, max_retries=0)
        cfg = ReportConfig(key="order", enabled=True, name="DonDatHang", folder="03",
                           url="https://example.test/order", normalize_units=True)
        source = order({"stt": 1, "ma_sp": "SP1", "ten_dvt": "Chai", "so_luong": 48})
        pages = [Response({"data": source}), Response({"data": []})] * 2
        with patch("src.mobiwork.load_conversions", return_value=CONVERSIONS) as loader,                 patch.object(MobiWorkClient, "_get_with_retry", side_effect=pages):
            first = client.fetch_report(cfg, date(2026, 10, 1))
            client.fetch_report(cfg, date(2026, 10, 2))
        self.assertEqual(first[0]["san_pham"][0]["so_luong"], 2.0)
        loader.assert_called_once()

    def test_unflagged_report_is_left_untouched(self):
        self.assertFalse(ReportConfig(key="bill", enabled=True, name="b", folder="b").normalize_units)

if __name__ == "__main__":
    unittest.main()
