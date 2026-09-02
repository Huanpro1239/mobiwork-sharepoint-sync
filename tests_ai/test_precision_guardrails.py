from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

from scoring.decision_policy import ScoringDecision, ScoreVector
from scoring.image_scoring import score_decoded_image_with_classification


class _YOLO:
    def __init__(self, *, signboard=False, bottle=False):
        self.signboard = signboard
        self.bottle = bottle

    def detect(self, _image):
        return {
            "bottles": [[10, 10, 40, 80]] if self.bottle else [],
            "packs": [],
            "signboards": [[20, 20, 300, 120]] if self.signboard else [],
        }


class _MustNotRunYOLO:
    def detect(self, _image):
        raise AssertionError("detector must not run for deterministic decision")


class _OCR:
    def __init__(self, *, brand=False, store=False, text=""):
        self.brand = brand
        self.store = store
        self.text = text

    def extract_text(self, _image, _boxes=None):
        return self.text

    def has_brand_keyword(self, _text):
        return self.brand

    def has_store_keyword(self, _text):
        return self.store


class _MustNotRunOCR:
    def extract_text(self, _image, _boxes=None):
        raise AssertionError("OCR must not run for deterministic decision")


class _Face:
    def has_face(self, _image):
        return False


class _MustNotRunFace:
    def has_face(self, _image):
        raise AssertionError("face audit must not run for deterministic decision")


def _neighbor(subcategory: str, similarity: float):
    return SimpleNamespace(
        effective_subcategory=subcategory,
        similarity=similarity,
    )


def _classification(
    scene: str,
    *,
    pass_probability: float,
    similarity: float,
    fraud_probability: float = 0.01,
    neighbors=(),
    pass_gate_passed: bool = True,
    auto_fail_gate_passed: bool = True,
):
    sign_probability = 0.95 if scene == "Bien_hieu" else 0.05
    decision = ScoringDecision(
        label=scene,
        scene=scene,
        status="PASS_CANDIDATE",
        score=pass_probability,
        reasons=("model candidate",),
    )
    scores = ScoreVector(
        sign_probability=sign_probability,
        pass_probability=pass_probability,
        fraud_probability=fraud_probability,
        reference_similarity=similarity,
    )
    return SimpleNamespace(
        decision=decision,
        scores=scores,
        neighbors=tuple(neighbors),
        quality_gate_passed=pass_gate_passed,
        auto_fail_gate_passed=auto_fail_gate_passed,
        sign_pass_probability=pass_probability,
        display_pass_probability=pass_probability,
    )


def _normal_image():
    rng = np.random.default_rng(20260902)
    return rng.integers(40, 220, size=(480, 640, 3), dtype=np.uint8)


class PrecisionGuardrailTests(unittest.TestCase):
    def test_auto_fail_uses_gate_carried_by_the_classification_result(self):
        score = score_decoded_image_with_classification(
            _normal_image(),
            _classification(
                "Trung_bay",
                pass_probability=0.10,
                similarity=0.90,
                auto_fail_gate_passed=False,
            ),
            _YOLO(),
            _OCR(),
            _Face(),
        )

        self.assertEqual(score.decision.label, "Can_duyet")
        self.assertEqual(score.decision.status, "REVIEW_QUALITY_GATE")

    def test_generic_store_sign_can_pass_when_human_model_support_is_strong(self):
        """Ground truth contains valid generic store signs without Vikoda brand text."""

        score = score_decoded_image_with_classification(
            _normal_image(),
            _classification("Bien_hieu", pass_probability=0.85, similarity=0.88),
            _YOLO(signboard=True),
            _OCR(store=True, text="Bách hóa tổng hợp Cô Bảy"),
            _Face(),
        )
        self.assertEqual(score.decision.label, "Bien_hieu")
        self.assertEqual(score.decision.status, "TIER1_HIGH_PASS")
        self.assertTrue(score.store_keyword)
        self.assertFalse(score.evidence.has_brand_keyword)

    def test_branded_sign_remains_valid_support_but_is_not_required(self):
        score = score_decoded_image_with_classification(
            _normal_image(),
            _classification("Bien_hieu", pass_probability=0.85, similarity=0.88),
            _YOLO(signboard=True),
            _OCR(brand=True, text="Vikoda Đảnh Thạnh"),
            _Face(),
        )
        self.assertEqual(score.decision.label, "Bien_hieu")
        self.assertEqual(score.decision.status, "TIER1_HIGH_PASS")

    def test_product_detection_cannot_rescue_a_weak_display_model_score(self):
        """A close-up Vikoda carton is human-labelled invalid display evidence."""

        score = score_decoded_image_with_classification(
            _normal_image(),
            _classification("Trung_bay", pass_probability=0.35, similarity=0.85),
            _YOLO(bottle=True),
            _OCR(),
            _Face(),
        )
        self.assertEqual(score.decision.label, "Can_duyet")
        self.assertEqual(score.decision.status, "TIER4_WEIGHTED_REVIEW")

    def test_two_close_human_display_references_can_confirm_moderate_score(self):
        score = score_decoded_image_with_classification(
            _normal_image(),
            _classification(
                "Trung_bay",
                pass_probability=0.55,
                similarity=0.86,
                neighbors=(
                    _neighbor("Dat/Trung bay", 0.88),
                    _neighbor("Dat/Trung bay", 0.84),
                ),
            ),
            _YOLO(),
            _OCR(),
            _Face(),
        )
        self.assertEqual(score.decision.label, "Trung_bay")
        self.assertEqual(score.decision.status, "TIER2_CONSENSUS_PASS")

    def test_doi_pho_neighbor_strengthens_fraud_rejection(self):
        score = score_decoded_image_with_classification(
            _normal_image(),
            _classification(
                "Trung_bay",
                pass_probability=0.80,
                similarity=0.86,
                fraud_probability=0.72,
                neighbors=(
                    _neighbor("Khong Dat/doi pho", 0.91),
                    _neighbor("Dat/Trung bay", 0.76),
                ),
            ),
            _YOLO(bottle=True),
            _OCR(),
            _Face(),
        )
        self.assertEqual(score.decision.label, "Khong_dat")
        self.assertEqual(score.decision.status, "TIER0_AUTO_FAIL_FRAUD")

    def test_severely_dark_image_cannot_make_ordinary_automatic_decision(self):
        dark = np.zeros((480, 640, 3), dtype=np.uint8)
        score = score_decoded_image_with_classification(
            dark,
            _classification("Bien_hieu", pass_probability=0.90, similarity=0.90),
            _YOLO(signboard=True),
            _OCR(store=True, text="Tạp hóa Minh"),
            _Face(),
        )
        self.assertEqual(score.decision.label, "Can_duyet")
        self.assertEqual(score.decision.status, "REVIEW_IMAGE_QUALITY")
        self.assertTrue(any("IMAGE_QUALITY" in warning for warning in score.audit_warnings))

    def test_hard_fraud_is_not_rescued_by_severe_image_quality(self):
        dark = np.zeros((480, 640, 3), dtype=np.uint8)
        score = score_decoded_image_with_classification(
            dark,
            _classification(
                "Bien_hieu",
                pass_probability=0.20,
                similarity=0.90,
                fraud_probability=0.92,
                neighbors=(
                    _neighbor("Khong Dat/doi pho", 0.93),
                    _neighbor("Khong Dat/doi pho", 0.89),
                ),
            ),
            _YOLO(),
            _OCR(),
            _Face(),
        )
        self.assertEqual(score.decision.label, "Khong_dat")
        self.assertEqual(score.decision.status, "TIER0_AUTO_FAIL_FRAUD")
        self.assertTrue(any("IMAGE_QUALITY" in warning for warning in score.audit_warnings))

    def test_tier0_fraud_skips_all_expensive_evidence_inference(self):
        score = score_decoded_image_with_classification(
            _normal_image(),
            _classification(
                "Trung_bay",
                pass_probability=0.15,
                similarity=0.90,
                fraud_probability=0.92,
                neighbors=(
                    _neighbor("Khong Dat/doi pho", 0.91),
                    _neighbor("Khong Dat/doi pho", 0.88),
                ),
            ),
            _MustNotRunYOLO(),
            _MustNotRunOCR(),
            _MustNotRunFace(),
        )
        self.assertEqual(score.decision.status, "TIER0_AUTO_FAIL_FRAUD")
        self.assertTrue(
            any("EVIDENCE_SKIPPED_DETERMINISTIC" in item for item in score.audit_warnings)
        )

    def test_tier1_human_consensus_skips_expensive_evidence_inference(self):
        score = score_decoded_image_with_classification(
            _normal_image(),
            _classification(
                "Bien_hieu",
                pass_probability=0.90,
                similarity=0.90,
                neighbors=(
                    _neighbor("Dat/Bien hieu", 0.92),
                    _neighbor("Dat/Bien hieu", 0.88),
                ),
            ),
            _MustNotRunYOLO(),
            _MustNotRunOCR(),
            _MustNotRunFace(),
        )
        self.assertEqual(score.decision.status, "TIER1_HIGH_PASS")
        self.assertEqual(score.decision.label, "Bien_hieu")

    def test_moderate_consensus_still_runs_full_evidence_path(self):
        score = score_decoded_image_with_classification(
            _normal_image(),
            _classification(
                "Trung_bay",
                pass_probability=0.55,
                similarity=0.86,
                neighbors=(
                    _neighbor("Dat/Trung bay", 0.88),
                    _neighbor("Dat/Trung bay", 0.84),
                ),
            ),
            _YOLO(bottle=True),
            _OCR(),
            _Face(),
        )
        self.assertEqual(score.decision.status, "TIER2_EVIDENCE_PASS")
        self.assertTrue(score.evidence.has_bottle_or_pack)


if __name__ == "__main__":
    unittest.main()
