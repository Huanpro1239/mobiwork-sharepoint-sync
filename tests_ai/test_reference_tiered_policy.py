from __future__ import annotations

import unittest
from types import SimpleNamespace

from scoring.decision_policy import DetectorEvidence, ScoreVector
from scoring.reference_decision_policy import decide_reference_tiered_scores


def _neighbor(subcategory: str, similarity: float):
    return SimpleNamespace(
        effective_subcategory=subcategory,
        similarity=similarity,
    )


class ReferenceTieredPolicyTests(unittest.TestCase):
    def test_hard_fraud_rejection(self):
        decision = decide_reference_tiered_scores(
            ScoreVector(0.90, 0.95, 0.88, 0.85),
            DetectorEvidence(has_signboard=True),
        )
        self.assertEqual(decision.label, "Khong_dat")
        self.assertEqual(decision.status, "TIER0_AUTO_FAIL_FRAUD")

    def test_doi_pho_neighbor_lowers_fraud_rejection_threshold(self):
        decision = decide_reference_tiered_scores(
            ScoreVector(0.10, 0.80, 0.72, 0.85),
            DetectorEvidence(has_bottle_or_pack=True),
            (
                _neighbor("Khong Dat/doi pho", 0.92),
                _neighbor("Dat/Trung bay", 0.75),
            ),
        )
        self.assertEqual(decision.label, "Khong_dat")
        self.assertEqual(decision.status, "TIER0_AUTO_FAIL_FRAUD")

    def test_generic_store_text_is_valid_sign_scene_evidence(self):
        decision = decide_reference_tiered_scores(
            ScoreVector(0.92, 0.85, 0.05, 0.88),
            DetectorEvidence(has_signboard=True),
            store_keyword=True,
        )
        self.assertEqual(decision.label, "Bien_hieu")
        self.assertEqual(decision.status, "TIER1_HIGH_PASS")

    def test_moderate_score_can_pass_from_human_reference_consensus(self):
        decision = decide_reference_tiered_scores(
            ScoreVector(0.05, 0.55, 0.10, 0.85),
            DetectorEvidence(),
            (
                _neighbor("Dat/Trung bay", 0.88),
                _neighbor("Dat/Trung bay", 0.84),
            ),
        )
        self.assertEqual(decision.label, "Trung_bay")
        self.assertEqual(decision.status, "TIER2_CONSENSUS_PASS")

    def test_detector_alone_cannot_manufacture_pass_from_low_model_score(self):
        decision = decide_reference_tiered_scores(
            ScoreVector(0.05, 0.20, 0.05, 0.84),
            DetectorEvidence(has_bottle_or_pack=True),
        )
        self.assertNotIn(decision.status, {"TIER1_HIGH_PASS", "TIER2_EVIDENCE_PASS"})
        self.assertNotEqual(decision.label, "Trung_bay")

    def test_two_failed_human_neighbors_support_clear_failure(self):
        decision = decide_reference_tiered_scores(
            ScoreVector(0.90, 0.25, 0.10, 0.82),
            DetectorEvidence(has_signboard=True),
            (
                _neighbor("Khong Dat/Khong dat bien hieu", 0.84),
                _neighbor("Khong Dat/Khong dat bien hieu", 0.80),
            ),
        )
        self.assertEqual(decision.label, "Khong_dat")
        self.assertEqual(decision.status, "TIER3_CLEAR_FAIL")

    def test_novel_image_goes_to_review(self):
        decision = decide_reference_tiered_scores(
            ScoreVector(0.90, 0.95, 0.01, 0.50),
            DetectorEvidence(has_signboard=True),
        )
        self.assertEqual(decision.label, "Can_duyet")
        self.assertEqual(decision.status, "REVIEW_NOVELTY")

    def test_automatic_pass_respects_oof_quality_gate(self):
        decision = decide_reference_tiered_scores(
            ScoreVector(0.90, 0.90, 0.01, 0.90),
            DetectorEvidence(has_signboard=True),
            pass_gate_passed=False,
        )
        self.assertEqual(decision.label, "Can_duyet")
        self.assertEqual(decision.status, "REVIEW_QUALITY_GATE")

    def test_automatic_failure_respects_oof_quality_gate(self):
        decision = decide_reference_tiered_scores(
            ScoreVector(0.05, 0.10, 0.90, 0.90),
            DetectorEvidence(has_bottle_or_pack=True),
            auto_fail_gate_passed=False,
        )
        self.assertEqual(decision.label, "Can_duyet")
        self.assertEqual(decision.status, "REVIEW_QUALITY_GATE")


if __name__ == "__main__":
    unittest.main()
