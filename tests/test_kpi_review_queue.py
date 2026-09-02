from __future__ import annotations

import unittest

import pandas as pd
from openpyxl import Workbook

from kpi.kpi_exporter import KPIExporter
from kpi.manual_labels import ManualLabelIndex
from kpi.review_queue import partition_review_rows, summarize_review_rows


class KPIReviewQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = pd.DataFrame(
            [
                {
                    "record_id": "needs-review",
                    "ten_nhan_vien": "NV NEEDS REVIEW",
                    "Phân Loại AI": "Can_duyet",
                    "Trạng Thái Quyết Định": "REVIEW_VALIDITY",
                    "hinh_anh": "https://example/review.jpg",
                },
                {
                    "record_id": "reviewed",
                    "ten_nhan_vien": "NV REVIEWED",
                    "Phân Loại AI": "Can_duyet",
                    "Trạng Thái Quyết Định": "REVIEW_FRAUD",
                    "hinh_anh": "https://example/reviewed.jpg",
                },
                {
                    "record_id": "pending",
                    "ten_nhan_vien": "NV PENDING",
                    "Phân Loại AI": "Khong_the_cham",
                    "Trạng Thái Quyết Định": "PENDING_SCORE",
                    "hinh_anh": "https://example/pending.jpg",
                },
                {
                    "record_id": "technical",
                    "ten_nhan_vien": "NV TECHNICAL",
                    "Phân Loại AI": "Khong_the_cham",
                    "Trạng Thái Quyết Định": "TECHNICAL_FAILURE",
                    "hinh_anh": "https://example/broken.jpg",
                },
                {
                    "record_id": "pass",
                    "ten_nhan_vien": "NV PASS",
                    "Phân Loại AI": "Bien_hieu",
                    "Trạng Thái Quyết Định": "AUTO_PASS_HIGH_CONFIDENCE",
                    "hinh_anh": "https://example/pass.jpg",
                },
                {
                    "record_id": "fail",
                    "ten_nhan_vien": "NV FAIL",
                    "Phân Loại AI": "Khong_dat",
                    "Trạng Thái Quyết Định": "AUTO_FAIL_LOW_EVIDENCE",
                    "hinh_anh": "https://example/fail.jpg",
                },
            ]
        )
        self.labels = ManualLabelIndex(
            by_record_id={"reviewed": "Khong_dat"},
            by_exact_key={},
            by_fallback_key={},
        )

    def test_partitions_manual_pending_and_technical_states_independently(self):
        partitions = partition_review_rows(self.frame, self.labels)

        self.assertEqual(partitions.manual_required["record_id"].tolist(), ["needs-review"])
        self.assertEqual(partitions.manual_resolved["record_id"].tolist(), ["reviewed"])
        self.assertEqual(partitions.pending["record_id"].tolist(), ["pending"])
        self.assertEqual(partitions.technical["record_id"].tolist(), ["technical"])

    def test_manual_label_excludes_review_row_from_required_queue(self):
        partitions = partition_review_rows(self.frame, self.labels)

        self.assertNotIn("reviewed", partitions.manual_required["record_id"].tolist())
        self.assertEqual(
            partitions.manual_resolved.iloc[0]["_manual_label"],
            "Khong_dat",
        )

    def test_summary_excludes_pending_and_technical_from_scored_denominator(self):
        summary = summarize_review_rows(self.frame, self.labels)

        self.assertEqual(summary["scored_decision_count"], 4)
        self.assertEqual(summary["manual_review_decision_count"], 2)
        self.assertEqual(summary["manual_review_required_count"], 1)
        self.assertEqual(summary["manual_review_resolved_count"], 1)
        self.assertEqual(summary["pending_score_count"], 1)
        self.assertEqual(summary["technical_failure_count"], 1)
        self.assertEqual(summary["auto_pass_count"], 1)
        self.assertEqual(summary["auto_fail_count"], 1)
        self.assertAlmostEqual(summary["manual_review_rate"], 0.5)
        self.assertAlmostEqual(summary["auto_pass_rate"], 0.25)

    def test_operational_unique_counts_prefer_url_over_shared_image_bytes(self):
        technical = pd.DataFrame(
            [
                {
                    "record_id": "broken-a",
                    "hinh_anh": "https://example/a.jpg",
                    "image_sha256": "same-bytes",
                    "Phân Loại AI": "Khong_the_cham",
                    "Trạng Thái Quyết Định": "TECHNICAL_FAILURE",
                },
                {
                    "record_id": "broken-b",
                    "hinh_anh": "https://example/b.jpg",
                    "image_sha256": "same-bytes",
                    "Phân Loại AI": "Khong_the_cham",
                    "Trạng Thái Quyết Định": "TECHNICAL_FAILURE",
                },
            ]
        )

        summary = summarize_review_rows(technical, ManualLabelIndex.empty())

        self.assertEqual(summary["technical_failure_unique"], 2)

    def test_alert_sheet_renders_three_separate_operational_sections(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Canh_bao"
        sheet.cell(1, 1, "2. DANH SÁCH ẢNH CẦN XỬ LÝ")
        sheet.cell(2, 1, "old header")
        sheet.cell(3, 1, "old data")
        sheet.cell(4, 1, "3. CẢNH BÁO KHÁC")

        KPIExporter._update_review_alerts(sheet, self.frame, self.labels)

        headings = {
            str(sheet.cell(row, 1).value): row
            for row in range(1, sheet.max_row + 1)
            if sheet.cell(row, 1).value
        }
        review_heading = next(key for key in headings if key.startswith("2A."))
        technical_heading = next(key for key in headings if key.startswith("2B."))
        pending_heading = next(key for key in headings if key.startswith("2C."))
        section_three = next(key for key in headings if key.startswith("3."))

        def employee_names(start_heading: str, end_heading: str) -> list[str]:
            return [
                str(sheet.cell(row, 2).value or "")
                for row in range(headings[start_heading] + 1, headings[end_heading])
            ]

        review_names = employee_names(review_heading, technical_heading)
        technical_names = employee_names(technical_heading, pending_heading)
        pending_names = employee_names(pending_heading, section_three)

        self.assertIn("NV NEEDS REVIEW", review_names)
        self.assertNotIn("NV REVIEWED", review_names)
        self.assertNotIn("NV PENDING", review_names)
        self.assertNotIn("NV TECHNICAL", review_names)
        self.assertIn("NV TECHNICAL", technical_names)
        self.assertIn("NV PENDING", pending_names)
        workbook.close()

    def test_rescore_changes_ai_column_but_preserves_manual_override_formula(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Chi_tiet_Anh_Checkin"
        for column in range(1, 31):
            sheet.cell(4, column, f"H{column}")
            sheet.cell(5, column, "")
        exporter = object.__new__(KPIExporter)
        rescored = pd.DataFrame(
            [
                {
                    "record_id": "reviewed",
                    "Phân Loại AI": "Bien_hieu",
                    "Trạng Thái Quyết Định": "AUTO_PASS",
                    "hinh_anh": "https://example/reviewed.jpg",
                }
            ]
        )

        exporter._replace_detail_rows(sheet, rescored, self.labels, customer_end_row=4)

        self.assertEqual(sheet["G5"].value, "Bien_hieu")
        self.assertEqual(sheet["H5"].value, "Khong_dat")
        self.assertEqual(sheet["I5"].value, '=IF(H5<>"",H5,G5)')
        workbook.close()


if __name__ == "__main__":
    unittest.main()
