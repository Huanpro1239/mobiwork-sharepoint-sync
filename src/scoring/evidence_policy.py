"""Production-only precision policy layered on top of the validated V2.3 model.

This module is deliberately separate from ``config.py`` and ``decision_policy.py``:
those files are part of the prebuilt model implementation hash.  Evidence rules can
therefore evolve and invalidate runtime score caches without pretending the trained
CLIP/logistic bundle itself was retrained.
"""
from __future__ import annotations

SIGN_AUTO_PASS_MIN = 0.92
SIGN_REFERENCE_SIMILARITY_MIN = 0.78
DISPLAY_AUTO_PASS_MIN = 0.95
DISPLAY_REFERENCE_SIMILARITY_MIN = 0.82

QUALITY_MIN_DIMENSION = 180
QUALITY_DARK_MEAN_MAX = 22.0
QUALITY_BRIGHT_MEAN_MIN = 245.0
QUALITY_BLUR_LAPLACIAN_MIN = 18.0

BRAND_OCR_KEYWORDS = (
    "vikoda",
    "đảnh thạnh",
    "danh thanh",
    "đảnh thạnh vikoda",
)

STORE_OCR_KEYWORDS = (
    "khánh hòa",
    "khanh hoa",
    "tạp hóa",
    "tap hoa",
    "cửa hàng",
    "cua hang",
    "siêu thị",
    "sieu thi",
    "đại lý",
    "dai ly",
    "nhà hàng",
    "nha hang",
    "quán",
    "quan",
    "store",
    "mart",
    "market",
    "bách hóa",
    "bach hoa",
)
