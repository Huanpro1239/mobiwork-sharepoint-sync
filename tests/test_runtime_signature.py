from __future__ import annotations

import unittest

import scoring.runtime_signature as runtime_signature


class RuntimeSignatureTests(unittest.TestCase):
    def test_model_signature_is_part_of_runtime_signature(self):
        first = runtime_signature.scoring_runtime_signature("model-a")
        second = runtime_signature.scoring_runtime_signature("model-b")
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertEqual(len(second), 64)

    def test_runtime_policy_version_invalidates_cached_scores(self):
        original = runtime_signature.SCORING_RUNTIME_VERSION
        try:
            first = runtime_signature.scoring_runtime_signature("same-model")
            runtime_signature.SCORING_RUNTIME_VERSION = original + "-changed"
            second = runtime_signature.scoring_runtime_signature("same-model")
        finally:
            runtime_signature.SCORING_RUNTIME_VERSION = original
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
