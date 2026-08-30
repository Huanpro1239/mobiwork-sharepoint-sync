from __future__ import annotations

import unittest

import numpy as np

from scoring.modeling import cross_validate_heads, score_embeddings, train_heads


SUBCATEGORIES = [
    "Dat/Bien hieu",
    "Khong Dat/Khong dat bien hieu",
    "Dat/Trung bay",
    "Khong Dat/Khong dat trung bay",
    "Khong Dat/doi pho",
]


class ModelingTests(unittest.TestCase):
    def _dataset(self, repetitions: int = 4):
        rng = np.random.default_rng(20260830)
        labels: list[str] = []
        groups: list[str] = []
        rows: list[np.ndarray] = []
        centers = np.eye(5, 8, dtype=np.float32)
        for label_index, label in enumerate(SUBCATEGORIES):
            for group_index in range(repetitions):
                rows.append(centers[label_index] + rng.normal(0, 0.02, size=8))
                labels.append(label)
                groups.append(f"{label_index}-{group_index}")
        features = np.asarray(rows, dtype=np.float32)
        features /= np.linalg.norm(features, axis=1, keepdims=True)
        return features, labels, groups

    def test_four_heads_train_and_score(self):
        features, labels, _ = self._dataset()
        heads = train_heads(features, labels)
        scores = score_embeddings(heads, features[:3])
        self.assertEqual(scores.sign_probability.shape, (3,))
        self.assertEqual(scores.sign_pass_probability.shape, (3,))
        self.assertEqual(scores.display_pass_probability.shape, (3,))
        self.assertEqual(scores.fraud_probability.shape, (3,))

    def test_model_head_names_are_stable(self):
        features, labels, _ = self._dataset()
        heads = train_heads(features, labels)
        self.assertEqual(heads.names, ("scene", "sign_validity", "display_validity", "fraud"))

    def test_group_aware_oof_evaluates_every_row(self):
        features, labels, groups = self._dataset(repetitions=4)
        report = cross_validate_heads(features, labels, groups, folds=4)
        self.assertEqual(report.total_count, len(labels))
        self.assertEqual(report.folds, 4)
        self.assertEqual(report.total_count, report.auto_decided_count + report.review_count)

    def test_too_few_independent_groups_fail_closed_to_review(self):
        features, labels, _ = self._dataset(repetitions=1)
        groups = [f"g-{index // 5}" for index in range(len(labels))]
        report = cross_validate_heads(features, labels, groups, folds=5)
        self.assertEqual(report.folds, 0)
        self.assertEqual(report.auto_decided_count, 0)
        self.assertEqual(report.review_count, len(labels))


if __name__ == "__main__":
    unittest.main()
