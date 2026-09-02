"""Shared inference path driven by model scores and human-reference evidence."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

import cv2
import numpy as np

from scoring.decision_policy import (
    DetectorEvidence,
    ScoringDecision,
    apply_detector_evidence,
)
from scoring.evidence_policy import (
    QUALITY_BLUR_LAPLACIAN_MIN,
    QUALITY_BRIGHT_MEAN_MIN,
    QUALITY_DARK_MEAN_MAX,
    QUALITY_MIN_DIMENSION,
)
from scoring.reference_decision_policy import (
    decide_reference_tiered_scores,
    neighbor_scene_consensus,
)


@dataclass(frozen=True)
class ImageScore:
    classification: Any
    detections: Mapping[str, list]
    ocr_text: str
    evidence: DetectorEvidence
    decision: ScoringDecision
    store_keyword: bool = False
    audit_warnings: tuple[str, ...] = ()


def _audit_warning(component: str, error: Exception) -> str:
    return f"{component}_AUDIT_ERROR {type(error).__name__}: {error}"


def _validate_image_bgr(image_bgr) -> None:
    if not isinstance(image_bgr, np.ndarray):
        raise TypeError("image_bgr must be a numpy array")
    if image_bgr.ndim != 3 or image_bgr.shape[2] != 3 or image_bgr.size == 0:
        raise ValueError("image_bgr must be a non-empty HxWx3 array")


def _quality_issue(image_bgr) -> str | None:
    """Return only severe quality problems that make automatic grading unsafe."""

    height, width = image_bgr.shape[:2]
    if min(height, width) < QUALITY_MIN_DIMENSION:
        return f"Ảnh quá nhỏ ({width}x{height})"

    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    mean_luma = float(np.mean(gray))
    if mean_luma <= QUALITY_DARK_MEAN_MAX:
        return f"Ảnh quá tối (brightness={mean_luma:.1f})"
    if mean_luma >= QUALITY_BRIGHT_MEAN_MIN:
        return f"Ảnh quá sáng (brightness={mean_luma:.1f})"

    laplacian_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if laplacian_variance < QUALITY_BLUR_LAPLACIAN_MIN:
        return f"Ảnh quá mờ (sharpness={laplacian_variance:.1f})"
    return None


def _apply_image_quality_guardrail(
    decision: ScoringDecision,
    quality_issue: str | None,
) -> ScoringDecision:
    """Review unusable images unless Tier-0 fraud evidence is already decisive.

    The local human-reference project orders hard fraud rejection before all other
    uncertainty handling.  Severe blur/darkness may block an ordinary pass/fail,
    but it must not rescue a decision already supported by the fraud head and/or a
    close ``doi pho`` human reference.  The quality problem remains in audit output.
    """

    if not quality_issue or decision.label == "Can_duyet":
        return decision
    if decision.status == "TIER0_AUTO_FAIL_FRAUD":
        return decision
    return replace(
        decision,
        label="Can_duyet",
        status="REVIEW_IMAGE_QUALITY",
        score=0.0,
        reasons=decision.reasons + (quality_issue,),
    )


def _empty_detections() -> dict[str, list]:
    return {"bottles": [], "packs": [], "signboards": []}


def _empty_evidence() -> DetectorEvidence:
    return DetectorEvidence(
        has_signboard=False,
        has_brand_keyword=False,
        has_bottle_or_pack=False,
        has_face=False,
    )


def _normalise_reference_subcategory(value: object) -> str:
    text = str(value or "").casefold().replace("\\", "/").replace("_", " ")
    return " ".join(text.split())


def _first_two_references_are_fail(neighbors: Sequence[Any]) -> bool:
    values = [
        _normalise_reference_subcategory(
            getattr(neighbor, "effective_subcategory", "")
        )
        for neighbor in tuple(neighbors or ())[:2]
    ]
    return len(values) == 2 and all(value.startswith("khong dat") for value in values)


def _deterministic_reference_decision(classification) -> ScoringDecision | None:
    """Return a decision only when detector/OCR/face cannot change the outcome.

    This fast path is deliberately narrower than the full policy. It skips costly
    evidence inference only for tiers whose ordering makes evidence irrelevant, or
    where the two nearest human-labelled references already supply the exact scene
    support required by the tier. Ambiguous scenes, Tier-2 and Tier-4 decisions
    always continue through the full evidence path.
    """

    scores = getattr(classification, "scores", None)
    if scores is None or classification.decision.status == "REVIEW_SCENE":
        return None

    decision = decide_reference_tiered_scores(
        scores,
        _empty_evidence(),
        getattr(classification, "neighbors", ()),
        store_keyword=False,
        pass_gate_passed=bool(getattr(classification, "quality_gate_passed", False)),
        auto_fail_gate_passed=bool(
            getattr(classification, "auto_fail_gate_passed", False)
        ),
        scene_override=classification.decision.scene,
        scene_ambiguous=False,
    )
    if decision.status in {
        "TIER0_AUTO_FAIL_FRAUD",
        "TIER0_REVIEW_FRAUD",
        "REVIEW_NOVELTY",
        # With empty physical evidence, Tier-1 can only pass because the two
        # nearest positive human references already confirm the scene.
        "TIER1_HIGH_PASS",
    }:
        return decision
    if decision.status == "TIER3_CLEAR_FAIL" and _first_two_references_are_fail(
        getattr(classification, "neighbors", ())
    ):
        return decision
    return None


def _ocr_flags(ocr, text: str) -> tuple[bool, bool]:
    has_brand_keyword = False
    has_store_keyword = False
    if hasattr(ocr, "has_brand_keyword"):
        has_brand_keyword = bool(ocr.has_brand_keyword(text))
    if hasattr(ocr, "has_store_keyword"):
        has_store_keyword = bool(ocr.has_store_keyword(text))
    elif hasattr(ocr, "has_brand_or_store_keyword"):
        # Old local adapters expose one broad helper.  Treat it as scene/store
        # evidence rather than claiming it is necessarily a Vikoda brand hit.
        has_store_keyword = bool(ocr.has_brand_or_store_keyword(text))
    return has_brand_keyword, has_store_keyword


def _resolve_ambiguous_scene(
    classification,
    classifier,
    evidence: DetectorEvidence,
    store_keyword: bool,
):
    """Resolve only scene ambiguity using physical or human-reference consensus."""

    if classification.decision.status != "REVIEW_SCENE" or classifier is None:
        return classification
    if not hasattr(classifier, "resolve_scene"):
        return classification

    sign_evidence = bool(
        evidence.has_signboard or evidence.has_brand_keyword or store_keyword
    )
    display_evidence = bool(evidence.has_bottle_or_pack and not sign_evidence)
    reference_hint = neighbor_scene_consensus(getattr(classification, "neighbors", ()))

    if sign_evidence:
        scene = "Bien_hieu"
        reason = "Scene mơ hồ được xác nhận bởi biển/tên cửa hàng"
    elif display_evidence:
        scene = "Trung_bay"
        reason = "Scene mơ hồ được xác nhận bởi cảnh có sản phẩm"
    elif reference_hint:
        scene = reference_hint
        reason = "Scene mơ hồ được xác nhận bởi hai mẫu người chấm gần nhất"
    else:
        return classification

    return classifier.resolve_scene(classification, scene, reason)


def score_decoded_image_with_classification(
    image_bgr,
    classification,
    yolo,
    ocr,
    face,
    image_rgb=None,
    classifier=None,
) -> ImageScore:
    """Combine frozen model output with detector/OCR and human-reference neighbours."""

    _validate_image_bgr(image_bgr)
    quality_issue = _quality_issue(image_bgr)

    deterministic = _deterministic_reference_decision(classification)
    if deterministic is not None:
        decision = _apply_image_quality_guardrail(deterministic, quality_issue)
        audit_warnings = [f"EVIDENCE_SKIPPED_DETERMINISTIC {deterministic.status}"]
        if quality_issue:
            audit_warnings.append(f"IMAGE_QUALITY {quality_issue}")
        return ImageScore(
            classification=classification,
            detections=_empty_detections(),
            ocr_text="",
            evidence=_empty_evidence(),
            decision=decision,
            store_keyword=False,
            audit_warnings=tuple(audit_warnings),
        )

    if image_rgb is None:
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)

    detections = yolo.detect(image_bgr)
    required_detection_keys = {"bottles", "packs", "signboards"}
    missing = required_detection_keys - set(detections)
    if missing:
        raise ValueError("YOLO result is missing keys: " + ", ".join(sorted(missing)))

    audit_warnings: list[str] = []
    text = ""
    has_brand_keyword = False
    has_store_keyword = False
    if (
        classification.decision.scene == "Bien_hieu"
        or classification.decision.status == "REVIEW_SCENE"
    ):
        try:
            text = ocr.extract_text(image_bgr, detections["signboards"])
            has_brand_keyword, has_store_keyword = _ocr_flags(ocr, text)
        except Exception as error:
            audit_warnings.append(_audit_warning("OCR", error))

    try:
        has_face = face.has_face(image_rgb)
    except Exception as error:
        # Face remains audit-only.  A face is common in valid field photos and
        # cannot by itself prove either validity or fraud.
        has_face = False
        audit_warnings.append(_audit_warning("FACE", error))

    evidence = DetectorEvidence(
        has_signboard=bool(detections["signboards"]),
        has_brand_keyword=has_brand_keyword,
        has_bottle_or_pack=bool(detections["bottles"] or detections["packs"]),
        has_face=has_face,
    )

    resolved_classification = _resolve_ambiguous_scene(
        classification,
        classifier,
        evidence,
        has_store_keyword,
    )
    scores = getattr(resolved_classification, "scores", None)
    if scores is not None:
        pass_gate_passed = bool(
            getattr(resolved_classification, "quality_gate_passed", False)
        )
        # Gate state belongs to the exact classification output.  Default false
        # for old score-bearing adapters so a missing field cannot authorize an
        # automatic business failure.
        auto_fail_gate_passed = bool(
            getattr(resolved_classification, "auto_fail_gate_passed", False)
        )
        decision = decide_reference_tiered_scores(
            scores,
            evidence,
            getattr(resolved_classification, "neighbors", ()),
            store_keyword=has_store_keyword,
            pass_gate_passed=pass_gate_passed,
            auto_fail_gate_passed=auto_fail_gate_passed,
            scene_override=resolved_classification.decision.scene,
            scene_ambiguous=(resolved_classification.decision.status == "REVIEW_SCENE"),
        )
    else:
        # Compatibility path for old lightweight adapters that do not expose the
        # learned score vector/reference neighbours.
        decision = apply_detector_evidence(resolved_classification.decision, evidence)

    decision = _apply_image_quality_guardrail(decision, quality_issue)
    if quality_issue:
        audit_warnings.append(f"IMAGE_QUALITY {quality_issue}")

    return ImageScore(
        classification=resolved_classification,
        detections=detections,
        ocr_text=text,
        evidence=evidence,
        decision=decision,
        store_keyword=has_store_keyword,
        audit_warnings=tuple(audit_warnings),
    )


def score_decoded_images(
    images_bgr: Sequence[np.ndarray],
    classifier,
    yolo,
    ocr,
    face,
) -> list[ImageScore | Exception]:
    """Score a batch with batched CLIP and isolated detector/OCR failures."""

    if not images_bgr:
        return []

    image_rgbs: list[np.ndarray | None] = [None] * len(images_bgr)
    results: list[ImageScore | Exception | None] = [None] * len(images_bgr)
    valid_indices: list[int] = []
    for index, image_bgr in enumerate(images_bgr):
        try:
            _validate_image_bgr(image_bgr)
            image_rgbs[index] = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
            valid_indices.append(index)
        except Exception as error:
            results[index] = error

    if valid_indices:
        valid_rgbs = [image_rgbs[index] for index in valid_indices]
        try:
            classifications = classifier.classify_batch(valid_rgbs)
            if len(classifications) != len(valid_indices):
                raise RuntimeError(
                    "classifier.classify_batch returned a different number of rows"
                )
        except Exception:
            classifications = []
            for image_rgb in valid_rgbs:
                try:
                    classifications.append(classifier.classify(image_rgb))
                except Exception as error:
                    classifications.append(error)

        for index, classification in zip(valid_indices, classifications, strict=True):
            if isinstance(classification, Exception):
                results[index] = classification
                continue
            try:
                results[index] = score_decoded_image_with_classification(
                    images_bgr[index],
                    classification,
                    yolo,
                    ocr,
                    face,
                    image_rgb=image_rgbs[index],
                    classifier=classifier,
                )
            except Exception as error:
                results[index] = error

    return [
        result if result is not None else RuntimeError("missing batch score result")
        for result in results
    ]


def score_decoded_image(image_bgr, classifier, yolo, ocr, face) -> ImageScore:
    """Backward-compatible single-image production path."""

    result = score_decoded_images([image_bgr], classifier, yolo, ocr, face)[0]
    if isinstance(result, Exception):
        raise result
    return result
