"""Image-scoring configuration.

The trained V2.3 bundle remains compatible, while production evidence policy is
versioned separately by :mod:`scoring.runtime_signature` so detector/OCR changes
invalidate cached decisions without forcing a model retrain.
"""
from __future__ import annotations

import os
from pathlib import Path

from project_paths import CACHE_DIR, REFERENCE_DIR, WEIGHTS_DIR

BASE_DIR = Path(__file__).resolve().parents[1]
REFERENCE_OVERRIDES = REFERENCE_DIR / "reference_overrides.csv"
CACHE_FILE = CACHE_DIR / "reference_bundle_v2.pkl"
YOLO_WEIGHTS = WEIGHTS_DIR / "yolov8s-world.pt"
DEVICE = os.environ.get("AI_DEVICE", "auto").strip().casefold() or "auto"
if DEVICE not in {"auto", "cpu", "cuda"}:
    raise ValueError("AI_DEVICE must be one of: auto, cpu, cuda")

PIPELINE_VERSION = "2.3.0"
CLIP_MODEL_ID = "openai/clip-vit-base-patch32"
CLIP_MODEL_REVISION = "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268"
CACHE_SCHEMA_VERSION = 4
CLIP_INFERENCE_BATCH_SIZE = 16

# Trained-model decision thresholds. Keep these stable until the reference bundle
# is deliberately rebuilt and OOF-validated.
AUTO_PASS_MIN = 0.88
AUTO_FAIL_MAX = 0.05
FRAUD_AUTO_FAIL_MIN = 0.975
FRAUD_REVIEW_MIN = 0.60
SCENE_MARGIN_MIN = 0.08
REFERENCE_SIMILARITY_MIN = 0.70
VISUAL_CONFLICT_SIMILARITY = 0.995
MODEL_CV_FOLDS = 5
QUALITY_GATE_MIN_PRECISION = 0.99
QUALITY_GATE_MIN_AUTO_PASS_COVERAGE = 0.20
QUALITY_GATE_MIN_GROUPS_PER_SUBCATEGORY = 3
QUALITY_GATE_MIN_AUTO_FAIL_SAMPLES = 10

# Production precision guardrails. These are intentionally stricter than the
# trained-model candidate thresholds: an automatic pass needs both model
# confidence and scene-specific business evidence. Otherwise it becomes review.
SIGN_AUTO_PASS_MIN = 0.92
SIGN_REFERENCE_SIMILARITY_MIN = 0.78
DISPLAY_AUTO_PASS_MIN = 0.95
DISPLAY_REFERENCE_SIMILARITY_MIN = 0.82
QUALITY_MIN_DIMENSION = 180
QUALITY_DARK_MEAN_MAX = 22.0
QUALITY_BRIGHT_MEAN_MIN = 245.0
QUALITY_BLUR_LAPLACIAN_MIN = 18.0

REFERENCE_CATEGORIES = {
    "Dat/Bien hieu": "Bien_hieu",
    "Dat/Trung bay": "Trung_bay",
    "Khong Dat/Khong dat bien hieu": "Khong_dat",
    "Khong Dat/Khong dat trung bay": "Khong_dat",
    "Khong Dat/doi pho": "Khong_dat",
}
LABELS = ["Bien_hieu", "Trung_bay", "Khong_dat"]
YOLO_CLASSES = [
    "bottle",
    "pack of bottles",
    "shelf",
    "cooler",
    "display rack",
    "floor",
    "signboard",
]
YOLO_CONFIDENCE = 0.25
FACE_CONFIDENCE = 0.5

# Only these words are brand evidence. Store-type text is useful for resolving a
# signboard scene but must never be treated as Vikoda/Đảnh Thạnh brand evidence.
BRAND_OCR_KEYWORDS = [
    "vikoda",
    "đảnh thạnh",
    "danh thanh",
    "đảnh thạnh vikoda",
]
STORE_OCR_KEYWORDS = [
    "khánh hòa", "khanh hoa",
    "tạp hóa", "tap hoa", "cửa hàng", "cua hang", "siêu thị", "sieu thi",
    "đại lý", "dai ly", "nhà hàng", "nha hang", "quán", "quan",
    "store", "mart", "market", "bách hóa", "bach hoa",
]
# Backward-compatible union for callers that only need general OCR vocabulary.
OCR_KEYWORDS = [*BRAND_OCR_KEYWORDS, *STORE_OCR_KEYWORDS]
