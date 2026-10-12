"""Regression tests for fail-closed promotion bonus reconciliation before publish."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import unittest

import pandas as pd

import promotion_bonus_calc as calc
from promotion_reconciliation_gate import ReconciliationGateError, evaluate


class PromotionReconciliationGateTests(unittest.TestCase):
    def setUp(self):
        self.results = [
            SimpleNamespace(
                program_name="CT ĐÚNG MỨC 1",
                rows=[
                    {"ma": "KH01", "_eligible": "Có",
                     "objTraThuong": {"SKU|Chai": 2},
                     "objThuongDuKien": {"SKU|Chai": 2},
                     "_rewards": [("SKU", "gift", "Chai", 2)]},
                    {"ma": "KH02", "_eligible": calc.REVIEW_DISPLAY,
                     "objTraThuong": {},
                     "objThuongDuKien": {"SKU|Chai": 3},
                     "_rewards": []},
                ],
            )
        ]
        rows = [
            {"Mức CT": "CT ĐÚNG MỨC 1", "Mã khách": code,
             "Trạng thái đăng ký CT": "Đã đăng ký",
             "Nguyên nhân không được tính": reason}
            for code, reason in [
                ("KH01", calc.ELIGIBLE_STATUS),
                ("KH02", calc.PENDING_DISPLAY),
            ]
        ]
        self.frames = {
            "DoanhSoChuaTinh": pd.DataFrame(
                rows, columns=calc.RECONCILIATION_COLUMNS
            ),
            "TraThuong": pd.DataFrame([
                {"Trạng thái": "Có", "Số lượng quà dự kiến": 2,
                 "Số lượng quà đủ điều kiện": 2,
                 "Tiền mặt dự kiến (đ)": 0, "Tiền mặt đủ điều kiện (đ)": 0},
                {"Trạng thái": calc.REVIEW_DISPLAY, "Số lượng quà dự kiến": 3,
                 "Số lượng quà đủ điều kiện": 0,
                 "Tiền mặt dự kiến (đ)": 0, "Tiền mặt đủ điều kiện (đ)": 0},
            ]),
        }
        self.manifest = {
            "customer_row_count": 2, "eligible_rows": 1, "review_display_rows": 1,
            "uncounted_rows": 2, "quality_status": "needs_review",
            "dms_equivalence_verified": False,
            "reward_coverage": {"cash_eligible_vnd": 0.0, "gift_reward_rows": 1},
        }

    def test_review_is_not_a_payout_approval(self):
        summary = evaluate(self.frames, self.results, self.manifest)
        self.assertEqual(summary["status"], "needs_review")
        self.assertEqual(summary["eligible_customer_rows"], 1)
        self.assertEqual(summary["registered_rows_checked"], 2)
        self.assertEqual(summary["review"]["deferred_reward_lines"], 1)
        self.assertEqual(summary["blocking_issues"], 0)

    def test_missing_registered_customer_blocks_publish_without_leaking_identity(self):
        frames = deepcopy(self.frames)
        frames["DoanhSoChuaTinh"] = frames["DoanhSoChuaTinh"].iloc[:1]
        manifest = {**self.manifest, "uncounted_rows": 1}
        with self.assertRaises(ReconciliationGateError) as raised:
            evaluate(frames, self.results, manifest)
        self.assertIn("registered_customer_coverage_mismatch", str(raised.exception))
        self.assertNotIn("KH02", str(raised.exception))

    def test_duplicate_registered_row_blocks_publish(self):
        frames = deepcopy(self.frames)
        frame = frames["DoanhSoChuaTinh"]
        frames["DoanhSoChuaTinh"] = pd.concat([frame, frame.iloc[:1]],
                                                ignore_index=True)
        with self.assertRaisesRegex(
            ReconciliationGateError, "manifest_count_mismatch:uncounted_rows"
        ):
            evaluate(frames, self.results, self.manifest)
        manifest = {**self.manifest, "uncounted_rows": 3}
        with self.assertRaisesRegex(
            ReconciliationGateError, "registered_customer_coverage_mismatch"
        ):
            evaluate(frames, self.results, manifest)

    def test_pending_customer_confirmed_object_blocks_publish(self):
        results = deepcopy(self.results)
        results[0].rows[1]["objTraThuong"] = {"SKU|Chai": 3}
        with self.assertRaisesRegex(ReconciliationGateError,
                                    "unapproved_customer_has_confirmed_reward"):
            evaluate(self.frames, results, self.manifest)

    def test_pending_customer_confirmed_ledger_blocks_publish(self):
        frames = deepcopy(self.frames)
        frames["TraThuong"].loc[1, "Số lượng quà đủ điều kiện"] = 3
        with self.assertRaisesRegex(ReconciliationGateError,
                                    "unapproved_ledger_has_confirmed_reward"):
            evaluate(frames, self.results, self.manifest)

    def test_manifest_total_mismatch_blocks_publish(self):
        manifest = {**self.manifest, "eligible_rows": 2}
        with self.assertRaisesRegex(ReconciliationGateError,
                                    "manifest_count_mismatch:eligible_rows"):
            evaluate(self.frames, self.results, manifest)

    def test_missing_reconciliation_column_blocks_publish(self):
        frames = deepcopy(self.frames)
        frames["DoanhSoChuaTinh"] = frames["DoanhSoChuaTinh"].drop(columns="Mã khách")
        with self.assertRaisesRegex(ReconciliationGateError,
                                    "invalid_reconciliation_21_column_contract"):
            evaluate(frames, self.results, self.manifest)

    def test_negative_or_invalid_reward_blocks_publish(self):
        frames = deepcopy(self.frames)
        frames["TraThuong"].loc[0, "Số lượng quà đủ điều kiện"] = -1
        with self.assertRaisesRegex(ReconciliationGateError,
                                    "invalid_reward_amount"):
            evaluate(frames, self.results, self.manifest)

    def test_eligible_cash_ledger_and_manifest_must_agree(self):
        frames = deepcopy(self.frames)
        frames["TraThuong"].loc[0, "Tiền mặt đủ điều kiện (đ)"] = 1000
        with self.assertRaisesRegex(ReconciliationGateError,
                                    "cash_eligible_total_mismatch"):
            evaluate(frames, self.results, self.manifest)

    def test_unregistered_buyer_is_classified_but_never_paid(self):
        frames = deepcopy(self.frames)
        extra = {col: None for col in calc.RECONCILIATION_COLUMNS}
        extra.update({
            "Mức CT": "CT ĐÚNG MỨC 1",
            "Mã khách": "UNREGISTERED",
            "Trạng thái đăng ký CT": "Chưa đăng ký",
            "Nguyên nhân không được tính": calc.UNREGISTERED,
        })
        frames["DoanhSoChuaTinh"] = pd.concat(
            [frames["DoanhSoChuaTinh"], pd.DataFrame([extra])],
            ignore_index=True,
        )
        manifest = {**self.manifest, "uncounted_rows": 3}
        summary = evaluate(frames, self.results, manifest)
        self.assertEqual(summary["review"]["unregistered"], 1)
        self.assertEqual(summary["eligible_customer_rows"], 1)
        self.assertNotIn("UNREGISTERED", repr(summary))

    def test_empty_programmes_still_produce_21_column_sheet(self):
        frames = {
            "DoanhSoChuaTinh": pd.DataFrame(columns=calc.RECONCILIATION_COLUMNS),
            "TraThuong": self.frames["TraThuong"].iloc[:0],
        }
        manifest = {
            **self.manifest, "customer_row_count": 0, "eligible_rows": 0,
            "review_display_rows": 0, "uncounted_rows": 0,
            "reward_coverage": {"cash_eligible_vnd": 0.0, "gift_reward_rows": 0},
        }
        summary = evaluate(frames, [], manifest)
        self.assertEqual(summary["registered_rows_checked"], 0)
        self.assertEqual(summary["reconciliation_rows"], 0)


if __name__ == "__main__":
    unittest.main()
