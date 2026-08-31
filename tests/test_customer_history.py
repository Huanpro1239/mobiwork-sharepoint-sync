from __future__ import annotations

from datetime import datetime
import unittest

import pandas as pd

from kpi.customer_aggregator import aggregate_customer_kpi
from kpi.customer_history import (
    apply_history_to_kpi,
    empty_history,
    load_rolling_kpi_inputs,
    update_customer_history,
)


class CustomerHistoryTests(unittest.TestCase):
    def test_history_preserves_earliest_activity_and_updates_latest(self):
        visits = pd.DataFrame(
            [
                {
                    "ma_kh": "KH001",
                    "ten_kh": "Khach A",
                    "_sync_date": "2025-01-10",
                }
            ]
        )
        orders = pd.DataFrame(
            [
                {
                    "ma_kh": "KH001",
                    "ten_kh": "Khach A",
                    "ngay_dat": "2024-12-20",
                }
            ]
        )
        first = update_customer_history(empty_history(), visits, orders)
        self.assertEqual(
            pd.Timestamp(first.loc[0, "first_activity_date"]),
            pd.Timestamp("2024-12-20"),
        )
        self.assertEqual(
            pd.Timestamp(first.loc[0, "first_visit_date"]),
            pd.Timestamp("2025-01-10"),
        )

        later_visits = pd.DataFrame(
            [
                {
                    "ma_kh": "KH001",
                    "ten_kh": "Khach A doi ten",
                    "_sync_date": "2026-08-25",
                }
            ]
        )
        updated = update_customer_history(first, later_visits, pd.DataFrame())
        self.assertEqual(
            pd.Timestamp(updated.loc[0, "first_activity_date"]),
            pd.Timestamp("2024-12-20"),
        )
        self.assertEqual(
            pd.Timestamp(updated.loc[0, "last_visit_date"]),
            pd.Timestamp("2026-08-25"),
        )
        self.assertEqual(updated.loc[0, "ten_kh"], "Khach A doi ten")

    def test_history_overrides_recent_window_for_new_old(self):
        visits = pd.DataFrame(
            [
                {
                    "ten_nhan_vien": "NV A",
                    "ngay": "2026-08-20",
                    "_sync_date": "2026-08-20",
                    "ma_kh": "KH001",
                    "ten_kh": "Khach A",
                    "ghi_ton": True,
                    "ghi_chu": "",
                }
            ]
        )
        orders = pd.DataFrame(
            columns=["ma_kh", "ngay_dat", "ma_dvt", "so_luong"]
        )
        recent = aggregate_customer_kpi(
            visits,
            orders,
            now=datetime(2026, 8, 30),
        )
        self.assertEqual(
            pd.Timestamp(recent.customers.loc[0, "first_activity_date"]),
            pd.Timestamp("2026-08-20"),
        )

        history = update_customer_history(
            empty_history(),
            pd.DataFrame(
                [
                    {
                        "ma_kh": "KH001",
                        "ten_kh": "Khach A",
                        "_sync_date": "2024-01-01",
                    }
                ]
            ),
            pd.DataFrame(),
        )
        merged = apply_history_to_kpi(recent, history)
        self.assertEqual(
            pd.Timestamp(merged.customers.loc[0, "first_activity_date"]),
            pd.Timestamp("2024-01-01"),
        )
        self.assertEqual(pd.Timestamp(merged.history_start), pd.Timestamp("2024-01-01"))

    def test_rolling_loader_downloads_only_previous_and_current_month(self):
        class FakeSource:
            def __init__(self):
                self.reads = []

            def _discover_report_workbooks(self, report_key, through):
                prefix = "01_BaoCaoViengTham" if report_key == "visit" else "03_DonDatHang"
                return [
                    f"{prefix}/2024/01/{prefix}_2024-01.xlsx",
                    f"{prefix}/2026/07/{prefix}_2026-07.xlsx",
                    f"{prefix}/2026/08/{prefix}_2026-08.xlsx",
                ]

            def _read_excel(self, path, sheet_name):
                self.reads.append((path, sheet_name))
                if sheet_name == "Data":
                    month = "2026-07-10" if "/07/" in path else "2026-08-10"
                    return pd.DataFrame(
                        [
                            {
                                "ten_nhan_vien": "NV",
                                "ma_kh": "KH1",
                                "ten_kh": "Khach",
                                "ngay": month,
                                "_sync_date": month,
                                "ghi_ton": True,
                                "ghi_chu": "",
                                "hinh_anh": "",
                                "stt_hinh": 1,
                            }
                        ]
                    )
                month = "2026-07-11" if "/07/" in path else "2026-08-11"
                return pd.DataFrame(
                    [
                        {
                            "ma_kh": "KH1",
                            "ten_kh": "Khach",
                            "ngay_dat": month,
                            "ten_nguoi_dat": "NV",
                            "ma_dvt": "Thùng",
                            "so_luong": 3,
                            "ma_phieu": "DH1",
                            "dien_giai": "",
                            "is_km": False,
                        }
                    ]
                )

        source = FakeSource()
        inputs = load_rolling_kpi_inputs(source, datetime(2026, 8, 30))
        self.assertEqual(len(inputs.visit_sources), 2)
        self.assertEqual(len(inputs.order_sources), 2)
        self.assertEqual(len(source.reads), 4)
        self.assertTrue(all("/2026/" in path for path, _ in source.reads))
        self.assertTrue(all("/2024/01/" not in path for path, _ in source.reads))


if __name__ == "__main__":
    unittest.main()
