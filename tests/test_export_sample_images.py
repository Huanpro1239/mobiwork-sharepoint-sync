from __future__ import annotations

import unittest

import pandas as pd

from export_sample_images import _safe_token, _scored_detail_rows, _suffix


class ExportSampleImagesTests(unittest.TestCase):
    def test_selects_only_resolved_scoring_rows(self):
        frame = pd.DataFrame(
            [
                {"record_id": "pass", "Trạng Thái Quyết Định": "TIER1_HIGH_PASS"},
                {"record_id": "fail", "Trạng Thái Quyết Định": "TIER0_AUTO_FAIL_FRAUD"},
                {"record_id": "review", "Trạng Thái Quyết Định": "TIER0_REVIEW_FRAUD"},
                {"record_id": "pending", "Trạng Thái Quyết Định": "PENDING_SCORE"},
                {"record_id": "tech", "Trạng Thái Quyết Định": "TECHNICAL_FAILURE"},
            ]
        )
        selected = _scored_detail_rows(frame)
        self.assertEqual(selected["record_id"].tolist(), ["pass", "fail", "review"])

    def test_duplicate_scored_record_id_is_rejected(self):
        frame = pd.DataFrame(
            [
                {"record_id": "same", "Trạng Thái Quyết Định": "TIER1_HIGH_PASS"},
                {"record_id": "same", "Trạng Thái Quyết Định": "TIER3_CLEAR_FAIL"},
            ]
        )
        with self.assertRaises(ValueError):
            _scored_detail_rows(frame)

    def test_safe_artifact_tokens_and_suffix(self):
        self.assertEqual(_safe_token("TIER0 AUTO/FAIL"), "TIER0_AUTO_FAIL")
        self.assertEqual(_suffix("Data anh/a/photo.PNG"), ".png")
        self.assertEqual(_suffix("Data anh/a/no-extension"), ".jpg")


if __name__ == "__main__":
    unittest.main()
