import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from monthly_master import (  # noqa: E402
    SYNC_DATE_COLUMN,
    _assert_partition_applied,
    build_month_from_partitions,
    frames_from_records,
    is_legacy_report_file,
    master_filename,
    merge_partition,
    month_dates_through,
    read_master,
    write_master,
)


class MonthlyMasterTests(unittest.TestCase):
    def test_master_filename_and_month_dates(self):
        target = date(2026, 8, 3)
        self.assertEqual(master_filename("BaoCaoViengTham", target), "BaoCaoViengTham_2026-08.xlsx")
        self.assertEqual(
            month_dates_through(target),
            [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)],
        )

    def test_flat_partition_replacement_keeps_other_days(self):
        master = build_month_from_partitions(
            [
                (date(2026, 8, 20), [{"id": "A", "value": 1}]),
                (date(2026, 8, 21), [{"id": "B", "value": 2}]),
            ],
            "flat",
        )
        incoming = frames_from_records(
            [{"id": "B2", "value": 99}],
            "flat",
            date(2026, 8, 21),
        )
        merged = merge_partition(master, incoming, date(2026, 8, 21), "flat")
        frame = merged["Data"]

        self.assertEqual(len(frame), 2)
        day20 = frame.loc[frame[SYNC_DATE_COLUMN] == "2026-08-20", "id"].tolist()
        day21 = frame.loc[frame[SYNC_DATE_COLUMN] == "2026-08-21", "id"].tolist()
        self.assertEqual(day20, ["A"])
        self.assertEqual(day21, ["B2"])

    def test_order_master_replaces_header_and_detail_partition(self):
        day20 = {
            "ma_phieu": "P20",
            "ngay_dat": "2026-08-20T01:00:00.000Z",
            "san_pham": [{"stt": 1, "ma_sp": "00008", "so_luong": "2"}],
        }
        day21 = {
            "ma_phieu": "P21",
            "ngay_dat": "2026-08-21T01:00:00.000Z",
            "san_pham": [{"stt": 1, "ma_sp": "00009", "so_luong": "3"}],
        }
        master = build_month_from_partitions(
            [(date(2026, 8, 20), [day20]), (date(2026, 8, 21), [day21])],
            "order",
        )
        replacement = {
            "ma_phieu": "P21X",
            "ngay_dat": "2026-08-21T02:00:00.000Z",
            "san_pham": [{"stt": 1, "ma_sp": "00010", "so_luong": "4"}],
        }
        incoming = frames_from_records([replacement], "order", date(2026, 8, 21))
        merged = merge_partition(master, incoming, date(2026, 8, 21), "order")

        self.assertEqual(merged["DonHang"]["ma_phieu"].tolist(), ["P20", "P21X"])
        self.assertEqual(merged["ChiTietSP"]["ma_phieu"].tolist(), ["P20", "P21X"])

    def test_order_master_upserts_existing_order_across_different_dates(self):
        day20 = {
            "ma_phieu": "P20",
            "ngay_dat": "2026-08-20T01:00:00.000Z",
            "trang_thai": "Chờ duyệt",
            "san_pham": [{"stt": 1, "ma_sp": "SP01", "so_luong": "2"}],
        }
        master = build_month_from_partitions(
            [(date(2026, 8, 20), [day20])],
            "order",
        )
        updated_p20_on_day21 = {
            "ma_phieu": "P20",
            "ngay_dat": "2026-08-20T01:00:00.000Z",
            "trang_thai": "Đã duyệt",
            "san_pham": [
                {"stt": 1, "ma_sp": "SP01", "so_luong": "5"},
                {"stt": 2, "ma_sp": "SP02", "so_luong": "1"},
            ],
        }
        incoming = frames_from_records([updated_p20_on_day21], "order", date(2026, 8, 21))
        merged = merge_partition(master, incoming, date(2026, 8, 21), "order")

        self.assertEqual(len(merged["DonHang"]), 1)
        self.assertEqual(merged["DonHang"]["ma_phieu"].tolist(), ["P20"])
        self.assertEqual(merged["DonHang"]["trang_thai"].tolist(), ["Đã duyệt"])
        self.assertEqual(merged["DonHang"][SYNC_DATE_COLUMN].tolist(), ["2026-08-21"])
        self.assertEqual(len(merged["ChiTietSP"]), 2)
        self.assertEqual(merged["ChiTietSP"]["ma_phieu"].tolist(), ["P20", "P20"])

    def test_customer_master_upserts_existing_makh_across_dates(self):
        day20 = {"makh": "KH01", "tenkh": "Cửa hàng A", "dia_chi": "Địa chỉ cũ"}
        master = build_month_from_partitions(
            [(date(2026, 8, 20), [day20])],
            "flat",
            upsert_keys=["makh"],
        )
        day21 = {"makh": "KH01", "tenkh": "Cửa hàng A", "dia_chi": "Địa chỉ mới"}
        incoming = frames_from_records([day21], "flat", date(2026, 8, 21))
        merged = merge_partition(
            master,
            incoming,
            date(2026, 8, 21),
            "flat",
            upsert_keys=["makh"],
        )

        frame = merged["Data"]
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame["makh"].tolist(), ["KH01"])
        self.assertEqual(frame["dia_chi"].tolist(), ["Địa chỉ mới"])
        self.assertEqual(frame[SYNC_DATE_COLUMN].tolist(), ["2026-08-21"])

    def test_flat_report_with_makh_does_not_upsert_without_config(self):
        master = build_month_from_partitions(
            [(date(2026, 8, 20), [{"makh": "KH01", "value": "old"}])],
            "flat",
        )
        incoming = frames_from_records(
            [{"makh": "KH01", "value": "new"}],
            "flat",
            date(2026, 8, 21),
        )
        merged = merge_partition(master, incoming, date(2026, 8, 21), "flat")

        self.assertEqual(len(merged["Data"]), 2)
        self.assertEqual(merged["Data"]["value"].tolist(), ["old", "new"])

    def test_configured_upsert_key_must_exist_in_incoming_data(self):
        master = build_month_from_partitions([], "flat", upsert_keys=["makh"])
        incoming = frames_from_records(
            [{"id": "A"}],
            "flat",
            date(2026, 8, 21),
        )
        with self.assertRaisesRegex(ValueError, "configured upsert key"):
            merge_partition(
                master,
                incoming,
                date(2026, 8, 21),
                "flat",
                upsert_keys=["makh"],
            )

    def test_quality_gate_rejects_dropped_flat_rows(self):
        target = date(2026, 8, 21)
        incoming = frames_from_records(
            [{"id": "A"}, {"id": "B"}],
            "flat",
            target,
        )
        broken = {"Data": incoming["Data"].iloc[:1].copy()}

        with self.assertRaisesRegex(RuntimeError, "quality gate failed"):
            _assert_partition_applied(broken, incoming, target, "flat", [])

    def test_quality_gate_rejects_dropped_configured_business_key(self):
        target = date(2026, 8, 21)
        incoming = frames_from_records(
            [{"ID": "C001"}, {"ID": "C002"}],
            "flat",
            target,
        )
        broken = {"Data": incoming["Data"].iloc[:1].copy()}

        with self.assertRaisesRegex(RuntimeError, "quality gate failed"):
            _assert_partition_applied(broken, incoming, target, "flat", ["ID"])

    def test_quality_gate_rejects_stale_order_detail_after_upsert(self):
        day20 = date(2026, 8, 20)
        day21 = date(2026, 8, 21)
        original = {
            "ma_phieu": "P20",
            "san_pham": [
                {"stt": 1, "ma_sp": "SP01", "so_luong": "2"},
                {"stt": 2, "ma_sp": "SP02", "so_luong": "1"},
            ],
        }
        updated = {
            "ma_phieu": "P20",
            "san_pham": [{"stt": 1, "ma_sp": "SP01", "so_luong": "5"}],
        }
        master = build_month_from_partitions([(day20, [original])], "order")
        incoming = frames_from_records([updated], "order", day21)
        merged = merge_partition(master, incoming, day21, "order")

        stale_detail = frames_from_records([original], "order", day20)["ChiTietSP"].iloc[[1]]
        broken = {name: frame.copy() for name, frame in merged.items()}
        broken["ChiTietSP"] = pd.concat(
            [broken["ChiTietSP"], stale_detail],
            ignore_index=True,
            sort=False,
        )

        with self.assertRaisesRegex(RuntimeError, "replaced order detail"):
            _assert_partition_applied(
                broken,
                incoming,
                day21,
                "order",
                ["ma_phieu"],
            )

    def test_order_month_rebuild_accepts_historical_lines_missing_stt(self):
        partitions = [
            (
                date(2026, 8, 1),
                [
                    {
                        "ma_phieu": "P01",
                        "san_pham": [
                            {"stt": None, "ma_sp": "A"},
                            {"stt": 2, "ma_sp": "B"},
                        ],
                    }
                ],
            ),
            (
                date(2026, 8, 2),
                [
                    {
                        "ma_phieu": "P02",
                        "san_pham": [
                            {"ma_sp": "C"},
                            {"stt": "", "ma_sp": "D"},
                        ],
                    }
                ],
            ),
        ]

        master = build_month_from_partitions(partitions, "order")
        detail = master["ChiTietSP"]

        self.assertEqual(len(master["DonHang"]), 2)
        self.assertEqual(len(detail), 4)
        self.assertFalse(detail[["ma_phieu", "stt"]].isna().any().any())
        self.assertFalse(detail[["ma_phieu", "stt"]].duplicated().any())
        self.assertEqual(
            detail.loc[detail["ma_phieu"] == "P02", "stt"].astype(int).tolist(),
            [1, 2],
        )

    def test_master_round_trip_preserves_sync_partition(self):
        frames = build_month_from_partitions(
            [(date(2026, 8, 21), [{"ma_kh": "00001", "ten_kh": "A"}])],
            "flat",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            current = Path.cwd()
            try:
                import os

                os.chdir(temp_dir)
                path = write_master(frames, "MoMoiKhachHang", date(2026, 8, 21))
                content = path.read_bytes()
            finally:
                os.chdir(current)

        loaded = read_master(content, "flat")
        self.assertIn(SYNC_DATE_COLUMN, loaded["Data"].columns)
        self.assertEqual(loaded["Data"][SYNC_DATE_COLUMN].astype(str).tolist(), ["2026-08-21"])

    def test_legacy_file_matching_is_conservative(self):
        canonical = "BaoCaoViengTham_2026-08.xlsx"
        self.assertFalse(
            is_legacy_report_file(canonical, "BaoCaoViengTham", canonical)
        )
        self.assertTrue(
            is_legacy_report_file(
                "BaoCaoViengTham_2026-08-21.xlsx",
                "BaoCaoViengTham",
                canonical,
            )
        )
        self.assertTrue(
            is_legacy_report_file(
                "BaoCaoViengTham_History_2026-08-01_to_2026-08-20.xlsx",
                "BaoCaoViengTham",
                canonical,
            )
        )
        self.assertTrue(
            is_legacy_report_file(
                "__sync_tmp_abc__BaoCaoViengTham_2026-08-21.xlsx",
                "BaoCaoViengTham",
                canonical,
            )
        )
        self.assertFalse(is_legacy_report_file("notes.xlsx", "BaoCaoViengTham", canonical))


if __name__ == "__main__":
    unittest.main()
