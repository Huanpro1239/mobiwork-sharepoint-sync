"""Central runtime paths for the sync + AI + KPI application.

Generated data and model assets live outside Git-tracked source files by default.
All locations can be overridden with environment variables for self-hosted runners.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = Path(os.environ.get("RUNTIME_ROOT", REPO_ROOT / "runtime")).expanduser().resolve()
AI_ASSET_ROOT = Path(os.environ.get("AI_ASSET_ROOT", RUNTIME_ROOT / "ai")).expanduser().resolve()
REFERENCE_DIR = Path(os.environ.get("AI_REFERENCE_DIR", AI_ASSET_ROOT / "reference")).expanduser().resolve()
WEIGHTS_DIR = Path(os.environ.get("AI_WEIGHTS_DIR", AI_ASSET_ROOT / "weights")).expanduser().resolve()
TEMPLATE_DIR = Path(os.environ.get("KPI_TEMPLATE_DIR", AI_ASSET_ROOT / "template")).expanduser().resolve()
CACHE_DIR = Path(os.environ.get("AI_CACHE_DIR", RUNTIME_ROOT / "cache")).expanduser().resolve()
OUTPUT_DIR = Path(os.environ.get("KPI_OUTPUT_DIR", RUNTIME_ROOT / "output")).expanduser().resolve()

TEMPLATE_EXCEL = Path(
    os.environ.get("KPI_TEMPLATE_FILE", TEMPLATE_DIR / "KPI_template.xlsx")
).expanduser().resolve()
OUTPUT_EXCEL = Path(
    os.environ.get("KPI_OUTPUT_FILE", OUTPUT_DIR / "Ket_qua_cham_cong_va_thuong_KPI.xlsx")
).expanduser().resolve()
SCORE_CACHE_DB = Path(
    os.environ.get("AI_SCORE_CACHE_DB", CACHE_DIR / "image_scores.sqlite3")
).expanduser().resolve()


def ensure_runtime_dirs() -> None:
    for path in (RUNTIME_ROOT, AI_ASSET_ROOT, REFERENCE_DIR, WEIGHTS_DIR, TEMPLATE_DIR, CACHE_DIR, OUTPUT_DIR):
        path.mkdir(parents=True, exist_ok=True)
