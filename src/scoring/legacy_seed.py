"""One-time cache seed from the previously reviewed image-score export.

This is intentionally opt-in and exists only to accelerate the first migration
run. It downloads the already stored SharePoint images, computes their SHA256,
and writes the historical decisions into the normal score cache. New images are
still scored by the active runtime.
"""
from __future__ import annotations

import csv
import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO

import main as core

from image_storage import ImageSharePointClient
from image_sync import ImageSyncConfig
from scoring.prebuilt_classifier import PrebuiltSceneClassifier
from scoring.score_cache import ScoreCache
from sharepoint_kpi_source import SharePointMonthlyKPISource


LOG = logging.getLogger("mobiwork_sync")


def _score(value: object) -> float | None:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number > 1.0:
        number /= 1000.0
    return max(0.0, min(number, 1.0))


def _payload(row: dict[str, str]) -> dict[str, object]:
    label = str(row.get("Phân Loại AI", "")).strip() or "Can_duyet"
    decision = str(row.get("Quyết Định", "")).strip() or "LEGACY_REVIEW"
    confidence = _score(row.get("Độ Tin Cậy AI"))
    scene = str(row.get("Loại Cảnh", "")).strip() or "Unknown"
    return {
        "Phân Loại AI": label,
        "Độ Tin Cậy AI": confidence,
        "Căn Cứ Nhận Diện": (
            str(row.get("Căn Cứ Nhận Diện", "")).strip()
            + " | MIGRATED_LEGACY_SCORE through 2026-08-28"
        ).strip(" |"),
        "Nội Dung Chữ OCR": str(row.get("Nội Dung Chữ OCR", "")).strip(),
        "Trạng Thái Quyết Định": decision,
        "Loại Cảnh": scene,
        "Điểm Scene": _score(row.get("Điểm Scene")),
        "Điểm Pass": _score(row.get("Điểm Pass")),
        "Điểm Fraud": _score(row.get("Điểm Fraud")),
        "Độ Tương Đồng Mẫu": _score(row.get("Độ Tương Đồng Mẫu")),
        "3 Tham Chiếu Gần Nhất": str(row.get("Mẫu Gần Nhất", "")).strip(),
        "Bằng Chứng Detector": "MIGRATED_LEGACY_SCORE; detector detail not present in legacy export",
        "Quality Gate": "legacy-reviewed-export",
        "sign_pass_probability": None,
        "display_pass_probability": None,
    }


def seed_legacy_scores(client: ImageSharePointClient) -> dict[str, int]:
    """Seed the normal SHA cache from an opt-in SharePoint legacy score CSV."""

    remote = os.environ.get("AI_LEGACY_SCORE_REMOTE", "").strip().strip("/")
    if not remote:
        return {"rows": 0, "seeded": 0, "missing": 0, "failed": 0}
    asset_drive = os.environ.get("AI_ASSET_DRIVE_ID", "").strip()
    data_drive = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
    if not asset_drive or not data_drive:
        raise RuntimeError("Legacy seed requires AI_ASSET_DRIVE_ID and SHAREPOINT_DRIVE_ID")

    raw = client.download_file_bytes(asset_drive, remote)
    if not raw:
        raise FileNotFoundError(f"Legacy score seed CSV missing: {remote}")
    text = raw.decode("utf-8-sig")
    rows = [
        dict(row)
        for row in csv.DictReader(StringIO(text))
        if str(row.get("hinh_anh", "")).strip()
        and str(row.get("Phân Loại AI", "")).strip()
    ]

    # One URL should represent one stored image. Keep the latest row if a legacy
    # export accidentally contains duplicate URL records.
    by_url: dict[str, dict[str, str]] = {}
    for row in rows:
        by_url[str(row["hinh_anh"]).strip()] = row
    unique_rows = list(by_url.values())

    reports = core.enabled_reports()
    source = SharePointMonthlyKPISource(client, data_drive, reports)
    cfg = ImageSyncConfig.from_env()
    signature = PrebuiltSceneClassifier().model_signature
    workers = max(1, int(os.environ.get("AI_LEGACY_SEED_WORKERS", "16")))

    stats = {"rows": len(unique_rows), "seeded": 0, "missing": 0, "failed": 0}

    def fetch(row: dict[str, str]):
        remote_image = source.resolve_image_path(row, cfg)
        content = client.download_file_bytes(data_drive, remote_image)
        if not content:
            raise FileNotFoundError(remote_image)
        digest = hashlib.sha256(content).hexdigest()
        return digest, _payload(row)

    with ScoreCache() as cache, ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch, row): row for row in unique_rows}
        for completed, future in enumerate(as_completed(futures), start=1):
            try:
                digest, payload = future.result()
                cache.put(signature, digest, payload)
                stats["seeded"] += 1
            except FileNotFoundError:
                stats["missing"] += 1
            except Exception as error:
                stats["failed"] += 1
                if stats["failed"] <= 20:
                    LOG.warning("Legacy score seed failed: %s: %s", type(error).__name__, error)
            if completed % 500 == 0:
                LOG.info(
                    "Legacy score seed progress: %s/%s seeded=%s missing=%s failed=%s",
                    completed,
                    len(unique_rows),
                    stats["seeded"],
                    stats["missing"],
                    stats["failed"],
                )

    LOG.info(
        "Legacy score seed complete: rows=%s seeded=%s missing=%s failed=%s",
        stats["rows"],
        stats["seeded"],
        stats["missing"],
        stats["failed"],
    )
    return stats
