from __future__ import annotations

import unittest

import pandas as pd

from kpi.customer_aggregator import aggregate_customer_kpi
from kpi.kpi_rules import is_valid_sign_note


class KPIV24Tests(unittest.TestCase):
    def _visits(self):
        return pd.DataFrame(
            [
                {"ten_nhan_vien": "NV A", "ngay": "2026-07-10", "ma_kh": "KH1", "ten_kh": "Cửa hàng 1", "ghi_ton": True, "ghi_chu": "Có biển hiệu", "hinh_anh": "x", "stt_hinh": 1},
                {"ten_nhan_vien": "NV A", "ngay": "2026-08-05", "ma_kh": "KH1", "ten_kh": "Cửa hàng 1", "ghi_ton": False, "ghi_chu": "", "hinh_anh": "y", "stt_hinh": 1},
                {"ten_nhan_vien": "NV A", "ngay": "2026-08-06", "ma_kh": "KH2", "ten_kh": "Cửa hàng 2", "ghi_ton": True, "ghi_chu": "Không biển bảng", "hinh_anh": "z", "stt_hinh": 1},
            ]
        )

    @staticmethod
    def _empty_orders():
        return pd.DataFrame(columns=["ma_kh", "ngay_dat", "ma_dvt", "so_luong"])

    def test_current_month_visit_is_required(self):
        visits = self._visits()
        visits.loc[len(visits)] = ["NV A", "2026-07-01", "KH_OLD_ONLY", "Old only", True, "", "a", 1]
        result = aggregate_customer_kpi(visits, self._empty_orders(), pd.Timestamp("2026-08-15"))
        self.assertNotIn("KH_OLD_ONLY", result.customers["ma_kh"].tolist())

    def test_same_day_rows_are_counted_as_separate_visit_events(self):
        visits = self._visits()
        visits.loc[len(visits)] = ["NV A", "2026-08-05", "KH1", "Cửa hàng 1", False, "", "second", 2]
        result = aggregate_customer_kpi(visits, self._empty_orders(), pd.Timestamp("2026-08-15"))
        row = result.customers.query("ma_kh == 'KH1'").iloc[0]
        self.assertEqual(int(row.visit_count_m), 2)

    def test_sync_date_controls_visit_business_month_for_utc_raw_timestamp(self):
        visits = pd.DataFrame(
            [
                {
                    "_sync_date": "2026-08-31",
                    "ten_nhan_vien": "NV A",
                    "ngay": "2026-08-31T17:00:00.000Z",
                    "ma_kh": "KH-END-MONTH",
                    "ten_kh": "Cuối tháng",
                    "ghi_ton": True,
                    "ghi_chu": "Có biển hiệu",
                    "hinh_anh": "x",
                    "stt_hinh": 1,
                }
            ]
        )
        result = aggregate_customer_kpi(
            visits,
            self._empty_orders(),
            pd.Timestamp("2026-08-30", tz="Asia/Ho_Chi_Minh"),
        )
        self.assertEqual(result.customers["ma_kh"].tolist(), ["KH-END-MONTH"])
        self.assertEqual(result.customers.iloc[0].first_activity_date, pd.Timestamp("2026-08-31"))

    def test_order_lines_are_collapsed_and_promo_is_excluded(self):
        orders = pd.DataFrame(
            [
                {"ma_kh": "KH1", "ngay_dat": "2026-08-01", "ma_dvt": "Thùng", "so_luong": 1.5, "ma_phieu": "DH1", "is_km": False},
                {"ma_kh": "KH1", "ngay_dat": "2026-08-01", "ma_dvt": "Két", "so_luong": 1.6, "ma_phieu": "DH1", "is_km": False},
                {"ma_kh": "KH1", "ngay_dat": "2026-08-01", "ma_dvt": "Thùng", "so_luong": 9, "ma_phieu": "DH1", "is_km": True},
            ]
        )
        row = aggregate_customer_kpi(self._visits(), orders, pd.Timestamp("2026-08-15")).customers.query("ma_kh == 'KH1'").iloc[0]
        self.assertAlmostEqual(float(row.max_order_2m_ktb), 3.1)
        self.assertAlmostEqual(float(row.total_order_2m_ktb), 3.1)
        self.assertEqual(int(row.order_count_2m), 1)

    def test_previous_month_stock_and_note_are_carried_forward(self):
        result = aggregate_customer_kpi(self._visits(), self._empty_orders(), pd.Timestamp("2026-08-15"))
        row = result.customers.query("ma_kh == 'KH1'").iloc[0]
        self.assertTrue(bool(row.ghi_ton_2m))
        self.assertTrue(bool(row.valid_sign_note_2m))

    def test_negative_sign_note_does_not_grant_exception(self):
        self.assertFalse(is_valid_sign_note("Không biển bảng"))
        self.assertTrue(is_valid_sign_note("Biển hiệu bị cây che một phần"))

    def test_first_activity_uses_full_supplied_history(self):
        visits = self._visits()
        visits.loc[len(visits)] = ["NV C", "2025-01-10", "KH2", "Cửa hàng 2", False, "", "", 1]
        result = aggregate_customer_kpi(visits, self._empty_orders(), pd.Timestamp("2026-08-15"))
        row = result.customers.query("ma_kh == 'KH2'").iloc[0]
        self.assertEqual(pd.Timestamp(row.first_activity_date), pd.Timestamp("2025-01-10"))


if __name__ == "__main__":
    unittest.main()
