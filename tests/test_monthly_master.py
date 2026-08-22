import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from monthly_master import (  # noqa: E402
    SYNC_DATE_COLUMN,
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
