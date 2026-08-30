"""High-level batch service around the audited V2.3 scoring components."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Iterable

import cv2
import numpy as np

from scoring.config import CLIP_INFERENCE_BATCH_SIZE
from scoring.score_cache import ScoreCache


@dataclass(frozen=True)
class ScoredBytes:
    image_sha256: str
    payload: dict[str, Any]
    cache_hit: bool


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _decode_bgr(content: bytes) -> np.ndarray:
    if not content:
        raise ValueError("image content is empty")
    array = np.frombuffer(content, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError("image bytes cannot be decoded by OpenCV")
    return image


def _flatten_image_score(score: Any) -> dict[str, Any]:
    decision = score.decision
    classification = score.classification
    scores = classification.scores
    evidence = score.evidence
    nearest = "; ".join(
        f"{neighbor.relative_path} [{neighbor.effective_subcategory}] ({neighbor.similarity:.3f})"
        for neighbor in classification.neighbors[:3]
    )
    evidence_text = "; ".join(
        (
            f"signboard={int(bool(evidence.has_signboard))}",
            f"brand_keyword={int(bool(evidence.has_brand_keyword))}",
            f"bottle_or_pack={int(bool(evidence.has_bottle_or_pack))}",
            f"face_audit={int(bool(evidence.has_face))}",
        )
    )
    audit_warnings = tuple(getattr(score, "audit_warnings", ()))
    if audit_warnings:
        evidence_text += "; audit_warning=" + " | ".join(audit_warnings)
    reasons = " | ".join(decision.reasons)
    confidence = (
        None
        if decision.label == "Can_duyet"
        else decision.score if decision.score > 0 else scores.pass_probability
    )
    return {
        "Phân Loại AI": decision.label,
        "Độ Tin Cậy AI": confidence,
        "Căn Cứ Nhận Diện": f"{decision.status}: {reasons} | {evidence_text}",
        "Nội Dung Chữ OCR": score.ocr_text,
        "Trạng Thái Quyết Định": decision.status,
        "Loại Cảnh": decision.scene,
        "Điểm Scene": round(float(scores.sign_probability), 6),
        "Điểm Pass": round(float(scores.pass_probability), 6),
        "Điểm Fraud": round(float(scores.fraud_probability), 6),
        "Độ Tương Đồng Mẫu": round(float(scores.reference_similarity), 6),
        "3 Tham Chiếu Gần Nhất": nearest,
        "Bằng Chứng Detector": evidence_text,
        "Quality Gate": bool(classification.quality_gate_passed),
        "sign_pass_probability": round(float(classification.sign_pass_probability), 6),
        "display_pass_probability": round(float(classification.display_pass_probability), 6),
    }


def technical_failure_payload(error: Exception | str) -> dict[str, Any]:
    detail = str(error)
    return {
        "Phân Loại AI": "Khong_the_cham",
        "Độ Tin Cậy AI": None,
        "Căn Cứ Nhận Diện": f"Lỗi kỹ thuật: {detail} [TECHNICAL_FAILURE]",
        "Nội Dung Chữ OCR": "",
        "Trạng Thái Quyết Định": "TECHNICAL_FAILURE",
        "Loại Cảnh": "Unknown",
        "Điểm Scene": None,
        "Điểm Pass": None,
        "Điểm Fraud": None,
        "Độ Tương Đồng Mẫu": None,
        "3 Tham Chiếu Gần Nhất": "",
        "Bằng Chứng Detector": "",
        "Quality Gate": "N/A",
        "sign_pass_probability": None,
        "display_pass_probability": None,
    }


class ImageScoringService:
    """Load model components once and score unique image bytes in CLIP batches."""

    def __init__(self, cache: ScoreCache | None = None) -> None:
        from scoring.face_detector import FaceDetector
        from scoring.ocr_engine import TargetedOCREngine
        from scoring.yolo_verifier import YOLODetector

        prebuilt = os.environ.get("AI_PREBUILT_BUNDLE", "false").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if prebuilt:
            from scoring.prebuilt_classifier import PrebuiltSceneClassifier

            self.classifier = PrebuiltSceneClassifier()
        else:
            from scoring.classifier import SceneClassifier

            self.classifier = SceneClassifier()
        self.yolo = YOLODetector()
        self.ocr = TargetedOCREngine()
        self.face = FaceDetector()
        self.cache = cache or ScoreCache()
        self.pipeline_signature = self.classifier.model_signature

    def close(self) -> None:
        self.cache.close()

    def score_contents(self, contents: Iterable[bytes]) -> list[ScoredBytes]:
        from scoring.image_scoring import score_decoded_images

        raw = list(contents)
        hashes = [sha256_bytes(content) for content in raw]
        results: list[ScoredBytes | None] = [None] * len(raw)
        pending_by_hash: dict[str, list[int]] = {}
        content_by_hash: dict[str, bytes] = {}

        for index, (content, digest) in enumerate(zip(raw, hashes, strict=True)):
            cached = self.cache.get(self.pipeline_signature, digest)
            if cached is not None:
                results[index] = ScoredBytes(digest, cached, True)
                continue
            pending_by_hash.setdefault(digest, []).append(index)
            content_by_hash.setdefault(digest, content)

        unique_hashes = list(pending_by_hash)
        for start in range(0, len(unique_hashes), CLIP_INFERENCE_BATCH_SIZE):
            batch_hashes = unique_hashes[start : start + CLIP_INFERENCE_BATCH_SIZE]
            images: list[np.ndarray] = []
            decodable_hashes: list[str] = []
            for digest in batch_hashes:
                try:
                    images.append(_decode_bgr(content_by_hash[digest]))
                    decodable_hashes.append(digest)
                except Exception as error:
                    payload = technical_failure_payload(error)
                    for index in pending_by_hash[digest]:
                        results[index] = ScoredBytes(digest, payload, False)

            if not images:
                continue
            outcomes = score_decoded_images(
                images,
                self.classifier,
                self.yolo,
                self.ocr,
                self.face,
            )
            if len(outcomes) != len(decodable_hashes):
                raise RuntimeError("score_decoded_images returned an unexpected number of results")
            for digest, outcome in zip(decodable_hashes, outcomes, strict=True):
                if isinstance(outcome, Exception):
                    payload = technical_failure_payload(outcome)
                else:
                    payload = _flatten_image_score(outcome)
                    self.cache.put(self.pipeline_signature, digest, payload)
                for index in pending_by_hash[digest]:
                    results[index] = ScoredBytes(digest, payload, False)

        if any(item is None for item in results):
            raise RuntimeError("image scoring left unresolved result rows")
        return [item for item in results if item is not None]

    def __enter__(self) -> "ImageScoringService":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
