from __future__ import annotations

import unittest

import pandas as pd

from score_kpi_pipeline import (
    _checkpoint_frame,
    _cleanup_scoring_checkpoints,
    _determine_run_status,
)


class CloudScoringCheckpointTests(unittest.TestCase):
    def test_checkpoint_cleanup_is_best_effort_after_canonical_publish(self):
        class Client:
            def __init__(self):
                self.paths = []

            def delete_path(self, _drive_id, remote_path):
                self.paths.append(remote_path)
                if remote_path.endswith("checkpoint.csv"):
                    raise RuntimeError("temporary cleanup failure")
                return True

        client = Client()

        removed = _cleanup_scoring_checkpoints(
            client,
            "drive",
            ("KPI/2026-08/scoring_checkpoint.csv", "KPI/2026-08/scoring_checkpoint_manifest.json"),
        )

        self.assertEqual(removed, 1)
        self.assertEqual(len(client.paths), 2)

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

    def test_checkpoint_excludes_retryable_technical_payload_even_with_sha(self):
        frame = pd.DataFrame(
            [
                {
                    "hinh_anh": "https://example/broken.jpg",
                    "pipeline_signature": "sig-v23",
                    "image_sha256": "abc",
                    "Trạng Thái Quyết Định": "TECHNICAL_FAILURE",
                },
                {
                    "hinh_anh": "https://example/review.jpg",
                    "pipeline_signature": "sig-v23",
                    "image_sha256": "def",
                    "Trạng Thái Quyết Định": "REVIEW_VALIDITY",
                },
            ]
        )

        checkpoint = _checkpoint_frame(frame, "sig-v23")

        self.assertEqual(checkpoint["hinh_anh"].tolist(), ["https://example/review.jpg"])

    def test_pending_unique_work_keeps_production_in_warmup(self):
        self.assertEqual(
            _determine_run_status(
                dry_run=False,
                pending_remaining=1,
                technical_failures=0,
            ),
            "warming_up",
        )

    def test_blocked_technical_errors_publish_with_explicit_degraded_status(self):
        self.assertEqual(
            _determine_run_status(
                dry_run=False,
                pending_remaining=0,
                technical_failures=2,
            ),
            "success_with_errors",
        )

    def test_manual_review_does_not_block_publish_and_dry_run_stays_successful(self):
        self.assertEqual(
            _determine_run_status(
                dry_run=False,
                pending_remaining=0,
                technical_failures=0,
            ),
            "success",
        )
        self.assertEqual(
            _determine_run_status(
                dry_run=True,
                pending_remaining=99,
                technical_failures=99,
            ),
            "success",
        )


if __name__ == "__main__":
    unittest.main()
