from __future__ import annotations

import unittest

import pandas as pd

from score_kpi_pipeline import _checkpoint_frame


class CloudScoringCheckpointTests(unittest.TestCase):
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

    def test_checkpoint_deduplicates_url_using_latest_record(self):
        frame = pd.DataFrame(
            [
                {
                    "hinh_anh": "https://example/a.jpg",
                    "pipeline_signature": "sig-v23",
                    "image_sha256": "old",
                },
                {
                    "hinh_anh": "https://example/a.jpg",
                    "pipeline_signature": "sig-v23",
                    "image_sha256": "new",
                },
            ]
        )
        checkpoint = _checkpoint_frame(frame, "sig-v23")
        self.assertEqual(len(checkpoint), 1)
        self.assertEqual(checkpoint.iloc[0]["image_sha256"], "new")


if __name__ == "__main__":
    unittest.main()
