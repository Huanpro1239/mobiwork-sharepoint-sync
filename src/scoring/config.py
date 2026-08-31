"""Image-scoring V2.3 configuration.

The thresholds are intentionally versioned and conservative. Runtime paths are
centralized in :mod:`project_paths`; model assets are never committed to Git.
This module intentionally avoids importing heavy CV runtimes so policy/model
unit tests stay lightweight.
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
OCR_KEYWORDS = [
    "vikoda", "đảnh thạnh", "danh thanh", "khánh hòa", "khanh hoa",
    "tạp hóa", "tap hoa", "cửa hàng", "cua hang", "siêu thị", "sieu thi",
    "đại lý", "dai ly", "nhà hàng", "nha hang", "quán", "quan",
    "store", "mart", "market", "bách hóa", "bach hoa",
]
