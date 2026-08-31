from __future__ import annotations

import unittest

from scoring.decision_policy import (
    DecisionPolicy,
    DetectorEvidence,
    ScoreVector,
    apply_detector_evidence,
    apply_quality_gates,
    decide_scores,
)


class DecisionPolicyTests(unittest.TestCase):
    def test_novelty_precedes_pass(self):
        decision = decide_scores(ScoreVector(0.9, 0.99, 0.01, 0.2))
        self.assertEqual(decision.status, "REVIEW_NOVELTY")

    def test_fraud_can_auto_fail_candidate(self):
        decision = decide_scores(ScoreVector(0.9, 0.99, 0.99, 0.9))
        self.assertEqual(decision.status, "AUTO_FAIL_FRAUD")

    def test_ambiguous_scene_requires_review(self):
        decision = decide_scores(ScoreVector(0.51, 0.99, 0.01, 0.9))
        self.assertEqual(decision.status, "REVIEW_SCENE")

    def test_pass_candidate_still_needs_detector_support(self):
        candidate = decide_scores(ScoreVector(0.9, 0.95, 0.01, 0.9))
        decision = apply_detector_evidence(candidate, DetectorEvidence())
        self.assertEqual(decision.label, "Can_duyet")
        self.assertEqual(decision.status, "REVIEW_MISSING_EVIDENCE")

    def test_sign_evidence_can_confirm_candidate(self):
        candidate = decide_scores(ScoreVector(0.9, 0.95, 0.01, 0.9))
        decision = apply_detector_evidence(candidate, DetectorEvidence(has_signboard=True))
        self.assertEqual(decision.label, "Bien_hieu")
        self.assertEqual(decision.status, "AUTO_PASS")

    def test_display_requires_pack_evidence(self):
        candidate = decide_scores(ScoreVector(0.1, 0.95, 0.01, 0.9))
        no_pack = apply_detector_evidence(candidate, DetectorEvidence(has_signboard=True))
        with_pack = apply_detector_evidence(candidate, DetectorEvidence(has_bottle_or_pack=True))
        self.assertEqual(no_pack.status, "REVIEW_MISSING_EVIDENCE")
        self.assertEqual(with_pack.status, "AUTO_PASS")

    def test_quality_gate_can_downgrade_pass_candidate(self):
        candidate = decide_scores(ScoreVector(0.9, 0.95, 0.01, 0.9))
        decision = apply_quality_gates(candidate, False, True)
        self.assertEqual(decision.status, "REVIEW_QUALITY_GATE")

    def test_invalid_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            DecisionPolicy(auto_fail_max=0.9, auto_pass_min=0.8)


if __name__ == "__main__":
    unittest.main()
