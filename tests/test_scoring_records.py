from __future__ import annotations

import unittest

from scoring.records import assign_record_ids, build_audit_record, technical_failure_payload


class ScoringRecordTests(unittest.TestCase):
    def test_duplicate_rows_get_distinct_stable_ids(self):
        row = {"ten_nhan_vien": "A", "ngay": "2026-08-01", "ma_kh": "KH1", "stt_hinh": 1, "hinh_anh": "https://x/a.jpg"}
        ids = assign_record_ids([row, dict(row)])
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(ids, assign_record_ids([row, dict(row)]))

    def test_technical_failure_is_not_business_fail(self):
        payload = technical_failure_payload("timeout")
        self.assertEqual(payload["Phân Loại AI"], "Khong_the_cham")
        self.assertEqual(payload["Trạng Thái Quyết Định"], "TECHNICAL_FAILURE")

    def test_audit_record_contains_reusable_payload(self):
        row = {"ten_nhan_vien": "A", "ngay": "2026-08-01", "ma_kh": "KH1", "ten_kh": "C1", "stt_hinh": 1, "hinh_anh": "https://x/a.jpg", "ghi_chu": ""}
        payload = {"Phân Loại AI": "Bien_hieu", "Trạng Thái Quyết Định": "AUTO_PASS"}
        record = build_audit_record(row, "rid", "sig", "sha", payload)
        self.assertEqual(record["image_sha256"], "sha")
        self.assertIn("score_payload_json", record)


if __name__ == "__main__":
    unittest.main()
