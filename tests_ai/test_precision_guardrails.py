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


class _Face:
    def has_face(self, _image):
        return False


def _classification(scene: str, *, pass_probability: float, similarity: float):
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
        fraud_probability=0.01,
        reference_similarity=similarity,
    )
    return SimpleNamespace(
        decision=decision,
        scores=scores,
        quality_gate_passed=True,
        sign_pass_probability=pass_probability,
        display_pass_probability=pass_probability,
    )


def _normal_image():
    rng = np.random.default_rng(20260902)
    return rng.integers(40, 220, size=(480, 640, 3), dtype=np.uint8)


class PrecisionGuardrailTests(unittest.TestCase):
    def test_generic_store_sign_is_not_brand_auto_pass(self):
        score = score_decoded_image_with_classification(
            _normal_image(),
            _classification("Bien_hieu", pass_probability=0.97, similarity=0.90),
            _YOLO(signboard=True),
            _OCR(store=True, text="Tạp hóa Minh"),
            _Face(),
        )
        self.assertEqual(score.decision.label, "Can_duyet")
        self.assertEqual(score.decision.status, "REVIEW_MISSING_BRAND_EVIDENCE")
        self.assertTrue(score.store_keyword)
        self.assertFalse(score.evidence.has_brand_keyword)

    def test_branded_sign_can_auto_pass_when_model_support_is_strong(self):
        score = score_decoded_image_with_classification(
            _normal_image(),
            _classification("Bien_hieu", pass_probability=0.97, similarity=0.90),
            _YOLO(signboard=True),
            _OCR(brand=True, text="Vikoda Đảnh Thạnh"),
            _Face(),
        )
        self.assertEqual(score.decision.label, "Bien_hieu")
        self.assertEqual(score.decision.status, "AUTO_PASS")

    def test_display_object_detection_alone_is_not_enough(self):
        score = score_decoded_image_with_classification(
            _normal_image(),
            _classification("Trung_bay", pass_probability=0.93, similarity=0.90),
            _YOLO(bottle=True),
            _OCR(),
            _Face(),
        )
        self.assertEqual(score.decision.label, "Can_duyet")
        self.assertEqual(score.decision.status, "REVIEW_EVIDENCE_STRENGTH")

    def test_severely_dark_image_cannot_auto_pass(self):
        dark = np.zeros((480, 640, 3), dtype=np.uint8)
        score = score_decoded_image_with_classification(
            dark,
            _classification("Bien_hieu", pass_probability=0.99, similarity=0.95),
            _YOLO(signboard=True),
            _OCR(brand=True, text="Vikoda"),
            _Face(),
        )
        self.assertEqual(score.decision.label, "Can_duyet")
        self.assertEqual(score.decision.status, "REVIEW_IMAGE_QUALITY")
        self.assertTrue(any("IMAGE_QUALITY" in warning for warning in score.audit_warnings))


if __name__ == "__main__":
    unittest.main()
