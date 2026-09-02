"""Version the complete scoring runtime, not only the trained classifier bundle.

The classifier bundle signature intentionally describes trained weights/reference data.
Production decisions also depend on detector/OCR/quality/evidence code.  If that code
changes while the bundle stays constant, old SHA-based scores must not be reused.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


SCORING_RUNTIME_VERSION = "2.4.0-precision-first"
_RUNTIME_FILES = (
    "config.py",
    "decision_policy.py",
    "image_scoring.py",
    "yolo_verifier.py",
    "ocr_engine.py",
    "face_detector.py",
    "service.py",
)


def scoring_runtime_signature(model_signature: str) -> str:
    """Return a cache key covering model assets plus all decision-time evidence code."""

    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    digest.update(str(model_signature).encode("utf-8"))
    digest.update(b"\0")
    digest.update(SCORING_RUNTIME_VERSION.encode("utf-8"))
    for filename in _RUNTIME_FILES:
        path = root / filename
        digest.update(b"\0file\0")
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()
