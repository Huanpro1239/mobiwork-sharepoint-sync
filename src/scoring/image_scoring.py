"""Shared inference path with precision-first evidence and image-quality contracts."""

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
    DISPLAY_AUTO_PASS_MIN,
    DISPLAY_REFERENCE_SIMILARITY_MIN,
    QUALITY_BLUR_LAPLACIAN_MIN,
    QUALITY_BRIGHT_MEAN_MIN,
    QUALITY_DARK_MEAN_MAX,
    QUALITY_MIN_DIMENSION,
    SIGN_AUTO_PASS_MIN,
    SIGN_REFERENCE_SIMILARITY_MIN,
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


def _apply_precision_guardrail(
    decision: ScoringDecision,
    classification,
    evidence: DetectorEvidence,
    quality_issue: str | None,
) -> ScoringDecision:
    """Downgrade uncertain automatic outcomes instead of guessing.

    The validated V2.3 model remains untouched.  This production layer is stricter:
    generic objects may resolve scene context, but only brand-specific sign evidence
    and sufficiently strong model/reference support may become AUTO_PASS.
    """

    if quality_issue and decision.status.startswith("AUTO_"):
        return replace(
            decision,
            label="Can_duyet",
            status="REVIEW_IMAGE_QUALITY",
            score=0.0,
            reasons=decision.reasons + (quality_issue,),
        )

    if decision.status != "AUTO_PASS":
        return decision

    scores = classification.scores
    if decision.scene == "Bien_hieu":
        if not (evidence.has_signboard and evidence.has_brand_keyword):
            return replace(
                decision,
                label="Can_duyet",
                status="REVIEW_MISSING_BRAND_EVIDENCE",
                score=0.0,
                reasons=decision.reasons
                + ("Biển hiệu chưa có bằng chứng Vikoda/Đảnh Thạnh đủ rõ",),
            )
        pass_min = SIGN_AUTO_PASS_MIN
        similarity_min = SIGN_REFERENCE_SIMILARITY_MIN
    else:
        if not evidence.has_bottle_or_pack:
            return replace(
                decision,
                label="Can_duyet",
                status="REVIEW_MISSING_EVIDENCE",
                score=0.0,
                reasons=decision.reasons + ("Thiếu bằng chứng chai/thùng trưng bày",),
            )
        pass_min = DISPLAY_AUTO_PASS_MIN
        similarity_min = DISPLAY_REFERENCE_SIMILARITY_MIN

    if (
        float(scores.pass_probability) < pass_min
        or float(scores.reference_similarity) < similarity_min
    ):
        return replace(
            decision,
            label="Can_duyet",
            status="REVIEW_EVIDENCE_STRENGTH",
            score=0.0,
            reasons=decision.reasons
            + (
                "Bằng chứng đúng loại nhưng chưa đủ mạnh để auto-pass "
                f"(pass={scores.pass_probability:.3f}, ref={scores.reference_similarity:.3f})",
            ),
        )
    return decision


def score_decoded_image_with_classification(
    image_bgr,
    classification,
    yolo,
    ocr,
    face,
    image_rgb=None,
    classifier=None,
) -> ImageScore:
    """Apply detector/OCR/face evidence to an already computed CLIP classification."""

    _validate_image_bgr(image_bgr)
    quality_issue = _quality_issue(image_bgr)
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
            if hasattr(ocr, "has_brand_keyword"):
                has_brand_keyword = bool(ocr.has_brand_keyword(text))
            else:
                # Compatibility with an older OCR adapter: broad text can help
                # scene review, but cannot satisfy the new brand-specific auto-pass
                # because has_brand_keyword remains false in that mode.
                has_brand_keyword = False
            if hasattr(ocr, "has_store_keyword"):
                has_store_keyword = bool(ocr.has_store_keyword(text))
            elif hasattr(ocr, "has_brand_or_store_keyword"):
                has_store_keyword = bool(ocr.has_brand_or_store_keyword(text))
        except Exception as error:
            audit_warnings.append(_audit_warning("OCR", error))

    try:
        has_face = face.has_face(image_rgb)
    except Exception as error:
        has_face = False
        audit_warnings.append(_audit_warning("FACE", error))

    evidence = DetectorEvidence(
        has_signboard=bool(detections["signboards"]),
        has_brand_keyword=has_brand_keyword,
        has_bottle_or_pack=bool(detections["bottles"] or detections["packs"]),
        has_face=has_face,
    )

    resolved_classification = classification
    if classification.decision.status == "REVIEW_SCENE":
        # Store text may identify a signboard scene, but only true brand OCR can
        # later confirm a sign AUTO_PASS.
        sign_strong = evidence.has_signboard and (
            evidence.has_brand_keyword or has_store_keyword
        )
        display_strong = (
            evidence.has_bottle_or_pack
            and not evidence.has_signboard
            and not evidence.has_brand_keyword
            and not has_store_keyword
        )
        if sign_strong and classifier is not None:
            resolved_classification = classifier.resolve_scene(
                classification,
                "Bien_hieu",
                "Scene mơ hồ được xác nhận bởi signboard + OCR",
            )
        elif display_strong and classifier is not None:
            resolved_classification = classifier.resolve_scene(
                classification,
                "Trung_bay",
                "Scene mơ hồ được xác nhận bởi bottle/pack và không có bằng chứng biển hiệu",
            )

    decision = apply_detector_evidence(resolved_classification.decision, evidence)
    decision = _apply_precision_guardrail(
        decision,
        resolved_classification,
        evidence,
        quality_issue,
    )
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
    """Score a batch with batched CLIP and per-image evidence isolation."""

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
