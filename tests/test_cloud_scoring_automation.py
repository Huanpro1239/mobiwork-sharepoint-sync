from __future__ import annotations

import hashlib
import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import score_kpi_pipeline as pipeline
from scoring.cloud_sample_compat import (
    _load_technical_attempts,
    _remote_scores_by_url,
    install_legacy_url_scoring,
)


AUTO_PASS_PAYLOAD = {
    "Phân Loại AI": "Bien_hieu",
    "Độ Tin Cậy AI": 0.99,
    "Căn Cứ Nhận Diện": "AUTO_PASS_HIGH_CONFIDENCE",
    "Nội Dung Chữ OCR": "",
    "Trạng Thái Quyết Định": "AUTO_PASS_HIGH_CONFIDENCE",
    "Loại Cảnh": "Sign",
    "Điểm Scene": 0.99,
    "Điểm Pass": 0.99,
    "Điểm Fraud": 0.01,
    "Độ Tương Đồng Mẫu": 0.9,
    "3 Tham Chiếu Gần Nhất": "",
    "Bằng Chứng Detector": "",
    "Quality Gate": True,
}


class FakeCache:
    last_seed_rows: list[dict[str, object]] = []

    def __init__(self, *_args, **_kwargs):
        pass

    def seed(self, rows, _signature):
        type(self).last_seed_rows = list(rows)
        return len(type(self).last_seed_rows)

    def get(self, _signature, _digest):
        return None

    def close(self):
        pass


class FakeService:
    seen_contents: list[bytes] = []
    pipeline_signature = "sig-current"

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def score_contents(self, contents):
        values = list(contents)
        type(self).seen_contents.extend(values)
        return [
            SimpleNamespace(
                image_sha256=hashlib.sha256(content).hexdigest(),
                payload=dict(AUTO_PASS_PAYLOAD),
                cache_hit=False,
            )
            for content in values
        ]


class FakeClient:
    def __init__(self, checkpoint_manifest=None):
        self.checkpoint_manifest = checkpoint_manifest

    def download_json(self, _drive_id, _remote_path):
        return self.checkpoint_manifest


class CloudScoringAutomationTests(unittest.TestCase):
    def setUp(self):
        self.original_builder = pipeline._build_image_results
        self.original_loader = pipeline._load_remote_score_rows
        self.original_downloader = pipeline._download_image_rows
        FakeService.seen_contents = []

    def tearDown(self):
        pipeline._build_image_results = self.original_builder
        pipeline._load_remote_score_rows = self.original_loader
        pipeline._download_image_rows = self.original_downloader
        FakeCache.last_seed_rows = []

    def _install(self, legacy, downloads, checkpoint_manifest=None, limit="1"):
        client = FakeClient(checkpoint_manifest)
        stack = ExitStack()
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "AI_PRODUCTION_MAX_PENDING_IMAGES": limit,
                    "AI_PRODUCTION_MAX_TECHNICAL_RETRIES": "3",
                    "KPI_SHAREPOINT_ROOT": "KPI",
                },
                clear=False,
            )
        )
        stack.enter_context(
            patch("scoring.cloud_sample_compat._load_legacy_by_url", return_value=legacy)
        )
        stack.enter_context(patch("scoring.cloud_sample_compat.ScoreCache", FakeCache))
        stack.enter_context(patch("scoring.service.ImageScoringService", FakeService))
        pipeline._load_remote_score_rows = lambda *_args: []
        pipeline._download_image_rows = downloads
        install_legacy_url_scoring(client)
        return stack, client

    def test_remote_technical_payload_is_not_reused_as_a_scored_result(self):
        rows = [
            {
                "hinh_anh": "https://example/broken.jpg",
                "pipeline_signature": "sig-current",
                "image_sha256": "abc",
                "score_payload_json": '{"Trạng Thái Quyết Định":"TECHNICAL_FAILURE"}',
            },
            {
                "hinh_anh": "https://example/review.jpg",
                "pipeline_signature": "sig-current",
                "image_sha256": "def",
                "score_payload_json": '{"Trạng Thái Quyết Định":"REVIEW_VALIDITY"}',
            },
        ]

        reusable = _remote_scores_by_url(rows, "sig-current")

        self.assertEqual(set(reusable), {"https://example/review.jpg"})

    def test_remote_technical_payload_is_not_seeded_into_local_score_cache(self):
        remote_rows = [
            {
                "hinh_anh": "https://example/broken.jpg",
                "pipeline_signature": "sig-current",
                "image_sha256": "abc",
                "score_payload_json": '{"Trạng Thái Quyết Định":"TECHNICAL_FAILURE"}',
            },
            {
                "hinh_anh": "https://example/review.jpg",
                "pipeline_signature": "sig-current",
                "image_sha256": "def",
                "score_payload_json": '{"Trạng Thái Quyết Định":"REVIEW_VALIDITY"}',
            },
        ]
        stack, client = self._install({}, lambda *_args: [])
        with stack:
            pipeline._load_remote_score_rows = lambda *_args: remote_rows
            pipeline._build_image_results(
                object(), client, "drive", [], pd.Timestamp("2026-08-01")
            )

        self.assertEqual(len(FakeCache.last_seed_rows), 1)
        self.assertEqual(
            FakeCache.last_seed_rows[0]["hinh_anh"],
            "https://example/review.jpg",
        )

    def test_legacy_review_is_rescored_once_per_unique_url_and_fanned_out(self):
        review_url = "https://example/review.jpg"
        new_url = "https://example/new.jpg"
        legacy = {
            review_url: {
                "hinh_anh": review_url,
                "Phân Loại AI": "Can_duyet",
                "Quyết Định": "LEGACY_REVIEW",
            },
            "https://example/auto.jpg": {
                "hinh_anh": "https://example/auto.jpg",
                "Phân Loại AI": "Bien_hieu",
                "Quyết Định": "LEGACY_AUTO_REUSED",
            },
        }
        rows = [
            {"hinh_anh": review_url, "ngay": "2026-08-01", "stt_hinh": 1},
            {"hinh_anh": review_url, "ngay": "2026-08-02", "stt_hinh": 2},
            {"hinh_anh": "https://example/auto.jpg", "ngay": "2026-08-03", "stt_hinh": 3},
            {"hinh_anh": new_url, "ngay": "2026-08-04", "stt_hinh": 4},
        ]

        def downloads(_source, _client, _drive, selected_rows):
            self.assertEqual([row["hinh_anh"] for row in selected_rows], [review_url])
            return [("Data/review.jpg", b"review-content", None)]

        stack, client = self._install(legacy, downloads)
        with stack:
            frame, stats, _signature = pipeline._build_image_results(
                object(), client, "drive", rows, pd.Timestamp("2026-08-01")
            )

        review_rows = frame[frame["hinh_anh"].eq(review_url)]
        self.assertEqual(len(review_rows), 2)
        self.assertEqual(review_rows["Phân Loại AI"].tolist(), ["Bien_hieu", "Bien_hieu"])
        self.assertEqual(review_rows["image_sha256"].nunique(), 1)
        self.assertEqual(len(FakeService.seen_contents), 1)
        self.assertEqual(stats["legacy_rescore_candidates_unique"], 1)
        self.assertEqual(stats["legacy_auto_reused_unique"], 1)
        self.assertEqual(stats["production_batch_scored_images"], 2)
        self.assertEqual(stats["production_batch_scored_unique"], 1)
        self.assertEqual(stats["production_pending_remaining_unique"], 1)
        pending = frame.loc[frame["hinh_anh"].eq(new_url)].iloc[0]
        self.assertEqual(pending["Trạng Thái Quyết Định"], "PENDING_SCORE")

    def test_duplicate_group_tries_later_occurrence_when_first_path_is_missing(self):
        url = "https://example/duplicate.jpg"
        rows = [
            {"hinh_anh": url, "ngay": "2026-08-01", "stt_hinh": 1},
            {"hinh_anh": url, "ngay": "2026-08-02", "stt_hinh": 2},
        ]
        attempted_ordinals = []

        def downloads(_source, _client, _drive, selected_rows):
            ordinal = selected_rows[0]["stt_hinh"]
            attempted_ordinals.append(ordinal)
            if ordinal == 1:
                return [(None, None, FileNotFoundError("first copy missing"))]
            return [("Data/duplicate.jpg", b"duplicate-content", None)]

        stack, client = self._install({}, downloads)
        with stack:
            frame, stats, _signature = pipeline._build_image_results(
                object(), client, "drive", rows, pd.Timestamp("2026-08-01")
            )

        self.assertEqual(attempted_ordinals, [1, 2])
        self.assertEqual(frame["Phân Loại AI"].tolist(), ["Bien_hieu", "Bien_hieu"])
        self.assertEqual(stats["missing_or_failed_images"], 0)
        self.assertEqual(stats["production_pending_remaining_unique"], 0)

    def test_compatibility_metrics_keep_row_counts_beside_unique_counts(self):
        rows = [
            {"hinh_anh": "https://example/a.jpg", "ngay": "2026-08-01", "stt_hinh": 1},
            {"hinh_anh": "https://example/b.jpg", "ngay": "2026-08-02", "stt_hinh": 2},
            {"hinh_anh": "https://example/b.jpg", "ngay": "2026-08-03", "stt_hinh": 3},
        ]

        def downloads(_source, _client, _drive, _selected_rows):
            return [("Data/a.jpg", b"a-content", None)]

        stack, client = self._install({}, downloads)
        with stack:
            _frame, stats, _signature = pipeline._build_image_results(
                object(), client, "drive", rows, pd.Timestamp("2026-08-01")
            )

        self.assertEqual(stats["production_batch_scored_images"], 1)
        self.assertEqual(stats["production_batch_scored_unique"], 1)
        self.assertEqual(stats["production_pending_remaining"], 2)
        self.assertEqual(stats["production_pending_remaining_unique"], 1)

    def test_retry_attempts_merge_checkpoint_and_canonical_manifest_durably(self):
        url_a = "https://example/a.jpg"
        url_b = "https://example/b.jpg"

        class Client:
            def download_json(self, _drive_id, remote_path):
                if remote_path.endswith("scoring_checkpoint_manifest.json"):
                    return {
                        "pipeline_signature": "sig-current",
                        "scoring": {"technical_attempts_by_url": {url_a: 1}},
                    }
                if remote_path.endswith("run_manifest.json"):
                    return {
                        "pipeline_signature": "sig-current",
                        "scoring": {
                            "technical_attempts_by_url": {url_a: 3, url_b: 2}
                        },
                    }
                return None

        attempts = _load_technical_attempts(
            Client(),
            "drive",
            pd.Timestamp("2026-08-01"),
            "sig-current",
        )

        self.assertEqual(attempts, {url_a: 3, url_b: 2})

    def test_retry_state_transport_error_fails_closed_instead_of_resetting(self):
        class Client:
            def download_json(self, _drive_id, _remote_path):
                raise RuntimeError("SharePoint unavailable")

        with self.assertRaisesRegex(RuntimeError, "SharePoint unavailable"):
            _load_technical_attempts(
                Client(),
                "drive",
                pd.Timestamp("2026-08-01"),
                "sig-current",
            )

    def test_third_source_failure_becomes_blocked_instead_of_warming_forever(self):
        url = "https://example/broken.jpg"
        rows = [{"hinh_anh": url, "ngay": "2026-08-01", "stt_hinh": 1}]
        checkpoint_manifest = {
            "pipeline_signature": "sig-current",
            "scoring": {"technical_attempts_by_url": {url: 2}},
        }

        def downloads(_source, _client, _drive, _selected_rows):
            return [(None, None, FileNotFoundError("missing"))]

        stack, client = self._install({}, downloads, checkpoint_manifest)
        with stack:
            frame, stats, _signature = pipeline._build_image_results(
                object(), client, "drive", rows, pd.Timestamp("2026-08-01")
            )

        self.assertEqual(frame.iloc[0]["Trạng Thái Quyết Định"], "TECHNICAL_FAILURE")
        self.assertEqual(stats["technical_attempts_by_url"], {url: 3})
        self.assertEqual(stats["production_pending_remaining_unique"], 0)
        self.assertEqual(stats["technical_failure_unique"], 1)

    def test_successive_checkpoints_reduce_five_unique_urls_three_one_zero(self):
        rows = [
            {
                "hinh_anh": f"https://example/{index}.jpg",
                "ngay": f"2026-08-0{index + 1}",
                "stt_hinh": index + 1,
            }
            for index in range(5)
        ]

        def downloads(_source, _client, _drive, selected_rows):
            return [
                (f"Data/{index}.jpg", row["hinh_anh"].encode(), None)
                for index, row in enumerate(selected_rows)
            ]

        stack, client = self._install({}, downloads, limit="2")
        remote_rows = []
        remaining = []
        with stack:
            pipeline._load_remote_score_rows = lambda *_args: remote_rows
            for _run_number in range(3):
                frame, stats, signature = pipeline._build_image_results(
                    object(), client, "drive", rows, pd.Timestamp("2026-08-01")
                )
                remaining.append(stats["production_pending_remaining_unique"])
                remote_rows = pipeline._checkpoint_frame(
                    frame, signature
                ).to_dict(orient="records")

        self.assertEqual(remaining, [3, 1, 0])
        self.assertEqual(len(FakeService.seen_contents), 5)


if __name__ == "__main__":
    unittest.main()
