from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scoring.score_cache import ScoreCache


class ScoreCacheTests(unittest.TestCase):
    def test_cache_is_scoped_by_model_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scores.sqlite3"
            with ScoreCache(path) as cache:
                cache.put("model-a", "sha", {"label": "Bien_hieu"})
                self.assertEqual(cache.get("model-a", "sha")["label"], "Bien_hieu")
                self.assertIsNone(cache.get("model-b", "sha"))

    def test_seed_requires_signature_and_payload(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            ScoreCache(Path(directory) / "scores.sqlite3") as cache,
        ):
            count = cache.seed(
                [
                    {"pipeline_signature": "m", "image_sha256": "a", "score_payload_json": json.dumps({"x": 1})},
                    {"pipeline_signature": "other", "image_sha256": "b", "score_payload_json": json.dumps({"x": 2})},
                ],
                "m",
            )
            self.assertEqual(count, 1)
            self.assertEqual(cache.get("m", "a"), {"x": 1})
            self.assertIsNone(cache.get("m", "b"))


if __name__ == "__main__":
    unittest.main()
