import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import production_smoke as smoke  # noqa: E402
from monthly_master import build_month_from_partitions, frames_from_records, read_master, write_master  # noqa: E402


class ProductionSmokeFrameTests(unittest.TestCase):
    def test_compare_report_frames_accepts_matching_partition(self):
        target = date(2026, 9, 1)
        expected = {
            "Data": pd.DataFrame(
                {
                    "_sync_date": ["2026-09-01", "2026-09-01"],
                    "ma_kh": ["A", "B"],
                    "qty": [1, 2],
                }
            )
        }
        actual = {
            "Data": pd.DataFrame(
                {
                    "_sync_date": ["2026-08-31", "2026-09-01", "2026-09-01"],
                    "ma_kh": ["OLD", "A", "B"],
                    "qty": [9, 1, 2],
                }
            )
        }

        result = smoke.compare_report_frames(actual, expected, target)
        self.assertEqual(result["compared_rows"], 2)

    def test_compare_report_frames_rejects_value_mismatch(self):
        target = date(2026, 9, 1)
        expected = {
            "Data": pd.DataFrame(
                {"_sync_date": ["2026-09-01"], "ma_kh": ["A"], "qty": [1]}
            )
        }
        actual = {
            "Data": pd.DataFrame(
                {"_sync_date": ["2026-09-01"], "ma_kh": ["A"], "qty": [99]}
            )
        }

        with self.assertRaisesRegex(AssertionError, "does not match fresh MobiWork source"):
            smoke.compare_report_frames(actual, expected, target)

    def test_compare_mixed_monthly_phone_column_to_numeric_looking_daily_phone(self):
        # Regression: before this fix, the monthly sheet read phones as strings
        # while the daily expected XLSX inferred numbers and removed leading zeroes.
        target = date(2026, 10, 10)
        monthly = build_month_from_partitions(
            [
                (date(2026, 10, 9), [{"ID": "old", "sdt": "HOTLINE"}]),
                (target, [{"ID": "0009", "sdt": "0984989830"}]),
            ],
            "flat",
            upsert_keys=["ID"],
        )
        daily = frames_from_records([{"ID": "0009", "sdt": "0984989830"}], "flat", target)
        with tempfile.TemporaryDirectory() as temporary:
            initial_dir = Path.cwd()
            try:
                os.chdir(temporary)
                actual_path = write_master(monthly, "Monthly", target)
                expected_path = write_master(daily, "Daily", target)
                actual = read_master(actual_path.read_bytes(), "flat")
                expected = read_master(expected_path.read_bytes(), "flat")
            finally:
                os.chdir(initial_dir)
        self.assertEqual(actual["Data"].iloc[1]["sdt"], "0984989830")
        self.assertEqual(expected["Data"].iloc[0]["sdt"], "0984989830")
        self.assertEqual(smoke.compare_report_frames(actual, expected, target)["compared_rows"], 1)

    def test_mismatch_still_fails_without_leaking_personal_values(self):
        target = date(2026, 10, 10)
        actual = {"Data": pd.DataFrame({
            "_sync_date": [target.isoformat()], "ID": ["0009"], "sdt": ["0984989830"],
        })}
        expected = {"Data": pd.DataFrame({
            "_sync_date": [target.isoformat()], "ID": ["0009"], "sdt": ["0984989831"],
        })}
        with self.assertRaises(AssertionError) as raised:
            smoke.compare_report_frames(actual, expected, target)
        message = str(raised.exception)
        self.assertIn("differing_columns=['sdt']", message)
        self.assertNotIn("0984989830", message)
        self.assertNotIn("0984989831", message)
        self.assertIn("does not match fresh MobiWork source", message)

    def test_compare_report_frames_rejects_stale_extra_values(self):
        target = date(2026, 9, 1)
        expected = {
            "Data": pd.DataFrame(
                {"_sync_date": ["2026-09-01"], "ma_kh": ["A"]}
            )
        }
        actual = {
            "Data": pd.DataFrame(
                {
                    "_sync_date": ["2026-09-01"],
                    "ma_kh": ["A"],
                    "removed_field": ["stale"],
                }
            )
        }

        with self.assertRaisesRegex(AssertionError, "stale extra values"):
            smoke.compare_report_frames(actual, expected, target)


ORDER_TARGET = date(2026, 10, 20)


def _order(ma_phieu, *, lines=2, **overrides):
    record = {
        "ma_phieu": ma_phieu,
        "ma_kh": "0001",
        "ma_nv_dat": "NV01",
        "ngay_tao": "2026-10-20T08:00:00+07:00",
        "ngay_dat": "2026-10-20T08:00:00+07:00",
        "ngay_sua": "2026-10-20T08:00:00+07:00",
        "ngay_duyet": None,
        "ma_nv_duyet": None,
        "trang_thai": "Chờ duyệt",
        "tong_tien_hang": 200000,
        "ck_don_hang": 0,
        "san_pham": [
            {"stt": stt, "ma_sp": f"SP{stt}", "so_luong": 2, "thanh_tien": 100000}
            for stt in range(1, lines + 1)
        ],
    }
    record.update(overrides)
    return record


def _approved(ma_phieu, **overrides):
    approval = {
        "trang_thai": "Đã duyệt",
        "ma_nv_duyet": "QL01",
        "ngay_duyet": "2026-10-21T10:15:00+07:00",
        "ngay_sua": "2026-10-21T10:15:00+07:00",
        "tong_tien_hang": 180000,
        "ck_don_hang": 20000,
    }
    return _order(ma_phieu, **{**approval, **overrides})


def _order_roundtrip(records):
    # Same path as production: frames -> XLSX -> read_master.
    frames = frames_from_records(records, "order", ORDER_TARGET)
    with tempfile.TemporaryDirectory() as temporary:
        initial_dir = Path.cwd()
        try:
            os.chdir(temporary)
            path = write_master(frames, "Order", ORDER_TARGET)
            return read_master(path.read_bytes(), "order")
        finally:
            os.chdir(initial_dir)


class ProductionSmokeOrderTests(unittest.TestCase):
    def compare(self, stored, fresh):
        return smoke.compare_report_frames(
            _order_roundtrip(stored), _order_roundtrip(fresh), ORDER_TARGET, "order"
        )

    def test_identical_orders_pass(self):
        result = self.compare([_order("DH1"), _order("DH2")], [_order("DH1"), _order("DH2")])
        self.assertEqual(result, {"compared_rows": 6, "revised_documents": 0})

    def test_post_sync_approval_and_edit_with_newer_revision_passes(self):
        fresh_edit = _approved("DH2", ngay_dat="2026-10-19T08:00:00+07:00", lines=3)
        fresh_edit["san_pham"][0]["so_luong"] = 5
        result = self.compare(
            [_order("DH1"), _order("DH2"), _order("DH3")],
            [_approved("DH1"), fresh_edit, _order("DH3")],
        )
        self.assertEqual(result["revised_documents"], 2)

    def test_approval_stamp_alone_explains_status_change(self):
        fresh = _approved("DH1", ngay_sua="2026-10-20T08:00:00+07:00")
        self.assertEqual(self.compare([_order("DH1")], [fresh])["revised_documents"], 1)

    def test_changed_values_without_newer_revision_fail(self):
        with self.assertRaisesRegex(AssertionError, r"Sheet DonHang:.*differing_columns=\['trang_thai'"):
            self.compare([_order("DH1")], [_order("DH1", trang_thai="Đã duyệt")])

    def test_older_fresh_revision_does_not_excuse_differences(self):
        stored = _approved("DH1")
        with self.assertRaisesRegex(AssertionError, "Sheet DonHang:"):
            self.compare([stored], [_order("DH1")])

    def test_missing_document_fails(self):
        with self.assertRaisesRegex(AssertionError, "missing_documents=1 extra_documents=0"):
            self.compare([_order("DH1")], [_order("DH1"), _approved("DH2")])

    def test_extra_document_fails(self):
        with self.assertRaisesRegex(AssertionError, "missing_documents=0 extra_documents=1"):
            self.compare([_order("DH1"), _order("DH2")], [_order("DH1")])

    def test_lost_line_on_unrevised_document_fails(self):
        with self.assertRaisesRegex(AssertionError, "missing_lines=1 extra_lines=0"):
            self.compare([_order("DH1", lines=1)], [_order("DH1", lines=2)])

    def test_line_value_change_on_unrevised_document_fails(self):
        fresh = _order("DH1")
        fresh["san_pham"][1]["so_luong"] = 7
        with self.assertRaisesRegex(AssertionError, r"Sheet ChiTietSP:.*'so_luong'"):
            self.compare([_order("DH1")], [fresh])

    def test_revised_document_still_checks_identity_columns(self):
        with self.assertRaisesRegex(AssertionError, r"identity columns.*'ma_kh'"):
            self.compare([_order("DH1")], [_approved("DH1", ma_kh="0002")])

    def test_revised_document_with_no_stored_lines_fails(self):
        with self.assertRaisesRegex(AssertionError, "revised document"):
            self.compare(
                [_order("DH1", lines=0), _order("DH2")],
                [_approved("DH1"), _order("DH2")],
            )

    def test_order_mismatch_message_keeps_values_private(self):
        with self.assertRaises(AssertionError) as raised:
            self.compare([_order("DH1")], [_order("DH1", ma_kh="0984989831")])
        self.assertNotIn("0984989831", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
