from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import pandas as pd

from score_kpi_pipeline import _checkpoint_frame
from scoring.cloud_sample_compat import (
    _production_pending_limit,
    _remote_scores_by_url,
)


class CloudScoringCheckpointTests(unittest.TestCase):
    def test_production_limit_is_validated(self):
        with patch.dict(os.environ, {"AI_PRODUCTION_MAX_PENDING_IMAGES": "4000"}):
            self.assertEqual(_production_pending_limit(), 4000)
        with (
            patch.dict(os.environ, {"AI_PRODUCTION_MAX_PENDING_IMAGES": "-1"}),
            self.assertRaises(ValueError),
        ):
            _production_pending_limit()

    def test_remote_rows_are_reused_only_for_current_signature(self):
        payload = {"Phân Loại AI": "Bien_hieu", "Trạng Thái Quyết Định": "AUTO_PASS"}
        rows = [
            {
                "hinh_anh": "https://example/a.jpg",
                "pipeline_signature": "sig-v23",
                "image_sha256": "abc",
                "score_payload_json": json.dumps(payload),
                "Tên File": "a.jpg",
            },
            {
                "hinh_anh": "https://example/b.jpg",
                "pipeline_signature": "old-sig",
                "image_sha256": "def",
                "score_payload_json": json.dumps(payload),
            },
        ]
        reusable = _remote_scores_by_url(rows, "sig-v23")
        self.assertEqual(set(reusable), {"https://example/a.jpg"})
        self.assertEqual(reusable["https://example/a.jpg"]["image_sha256"], "abc")

    def test_checkpoint_keeps_only_reusable_scored_urls(self):
        frame = pd.DataFrame(
            [
                {
                    "hinh_anh": "https://example/a.jpg",
                    "pipeline_signature": "sig-v23",
                    "image_sha256": "abc",
                },
                {
                    "hinh_anh": "https://example/pending.jpg",
                    "pipeline_signature": "sig-v23",
                    "image_sha256": "",
                },
                {
                    "hinh_anh": "https://example/old.jpg",
                    "pipeline_signature": "old-sig",
                    "image_sha256": "def",
                },
            ]
        )
        checkpoint = _checkpoint_frame(frame, "sig-v23")
        self.assertEqual(checkpoint["hinh_anh"].tolist(), ["https://example/a.jpg"])


if __name__ == "__main__":
    unittest.main()
