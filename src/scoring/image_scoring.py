"""Shared inference path with explicit image color and evidence contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import cv2
import numpy as np

from scoring.decision_policy import (
    DetectorEvidence,
    ScoringDecision,
    apply_detector_evidence,
)


@dataclass(frozen=True)
class ImageScore:
    classification: Any
    detections: Mapping[str, list]
    ocr_text: str
    evidence: DetectorEvidence
    decision: ScoringDecision
    audit_warnings: tuple[str, ...] = ()


def _audit_warning(component: str, error: Exception) -> str:
    return f"{component}_AUDIT_ERROR {type(error).__name__}: {error}"


def _validate_image_bgr(image_bgr) -> None:
    if not isinstance(image_bgr, np.ndarray):
        raise TypeError("image_bgr must be a numpy array")
    if image_bgr.ndim != 3 or image_bgr.shape[2] != 3 or image_bgr.size == 0:
        raise ValueError("image_bgr must be a non-empty HxWx3 array")


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
    if image_rgb is None:
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)

    detections = yolo.detect(image_bgr)
    required_detection_keys = {"bottles", "packs", "signboards"}
    missing = required_detection_keys - set(detections)
    if missing:
        raise ValueError(f"YOLO result is missing keys: {', '.join(sorted(missing))}")

    audit_warnings: list[str] = []
    text = ""
    has_brand_keyword = False
    if (
        classification.decision.scene == "Bien_hieu"
        or classification.decision.status == "REVIEW_SCENE"
    ):
        try:
            text = ocr.extract_text(image_bgr, detections["signboards"])
            has_brand_keyword = ocr.has_brand_or_store_keyword(text)
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
        sign_strong = evidence.has_signboard and evidence.has_brand_keyword
        display_strong = (
            evidence.has_bottle_or_pack
            and not evidence.has_signboard
            and not evidence.has_brand_keyword
        )
        if sign_strong and classifier is not None:
            resolved_classification = classifier.resolve_scene(
                classification,
                "Bien_hieu",
                "Scene mơ hồ được xác nhận bởi signboard + OCR keyword",
            )
        elif display_strong and classifier is not None:
            resolved_classification = classifier.resolve_scene(
                classification,
                "Trung_bay",
                "Scene mơ hồ được xác nhận bởi bottle/pack và không có bằng chứng biển hiệu",
            )

    decision = apply_detector_evidence(resolved_classification.decision, evidence)
    return ImageScore(
        classification=resolved_classification,
        detections=detections,
        ocr_text=text,
        evidence=evidence,
        decision=decision,
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
