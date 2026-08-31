"""Robust stored-image lookup for cloud KPI scoring.

Image sync filenames contain a stable SHA256 digest of the original MobiWork
URL. Monthly masters can contain a different/blank ``stt_hinh`` value than the
sequence used when the image was persisted, so cloud scoring must not require
an exact sequence match when locating an already-synced image.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import PurePosixPath
from typing import Any

from image_sync import ImageSyncConfig, _parse_date, _remote_image_path


LOG = logging.getLogger("mobiwork_sync")


def _digest_fallback_path(source: Any, row: dict[str, Any], cfg: ImageSyncConfig) -> str:
    image_date = _parse_date(row.get("_sync_date")) or _parse_date(row.get(cfg.date_field))
    if image_date is None:
        raise ValueError("Ảnh không có ngày hợp lệ")

    url = str(row.get(cfg.url_field, "")).strip()
    if not url:
        raise ValueError("Ảnh không có URL")

    # Extension and sequence do not affect the destination folder.  Use a
    # placeholder here and locate the final file by its immutable URL digest.
    folder, _ = _remote_image_path(
        cfg,
        row,
        url,
        image_date,
        int(row.get("_image_index") or 1),
        ".jpg",
    )
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    date_token = f"_{image_date:%Y%m%d}_"
    matches: list[str] = []
    for item in source._children(folder):
        if "folder" in item:
            continue
        name = str(item.get("name", "")).strip()
        stem = PurePosixPath(name).stem
        if date_token in stem and stem.endswith(f"_{digest}"):
            matches.append(name)

    if len(matches) == 1:
        remote = f"{folder}/{matches[0]}"
        LOG.info("Resolved stored image by URL digest fallback: %s", remote)
        return remote
    if not matches:
        raise FileNotFoundError(
            f"Không tìm thấy ảnh đã sync theo URL digest: folder={folder} digest={digest}"
        )
    raise RuntimeError(
        f"Có nhiều ảnh cùng URL digest/date trong folder {folder}: {', '.join(sorted(matches))}"
    )


def install_robust_image_path_lookup() -> None:
    """Patch the SharePoint KPI source with sequence-independent lookup."""

    from sharepoint_kpi_source import SharePointMonthlyKPISource

    original = SharePointMonthlyKPISource.resolve_image_path
    if getattr(original, "_url_digest_fallback", False):
        return

    def robust(self, row: dict[str, Any], cfg: ImageSyncConfig) -> str:
        try:
            return original(self, row, cfg)
        except FileNotFoundError:
            return _digest_fallback_path(self, row, cfg)

    robust._url_digest_fallback = True  # type: ignore[attr-defined]
    SharePointMonthlyKPISource.resolve_image_path = robust
