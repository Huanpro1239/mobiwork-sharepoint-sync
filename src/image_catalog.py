"""Locate rolling image-sync files already persisted in SharePoint.

The naming contract mirrors :mod:`image_sync`: month / employee / customer with a
URL hash in each filename. Scoring reads these stored bytes instead of redownloading
from MobiWork.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

_INVALID_SEGMENT_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_segment(value: Any, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    text = _INVALID_SEGMENT_RE.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text or fallback)[:120]


def image_prefix(
    record: dict[str, Any],
    image_url: str,
    image_date: date,
    image_index: int,
    *,
    employee_field: str = "ten_nhan_vien",
    customer_field: str = "ma_kh",
    sequence_field: str = "stt_hinh",
) -> tuple[str, str, str]:
    employee = safe_segment(record.get(employee_field), "Khong_ro_nhan_vien")
    customer = safe_segment(record.get(customer_field), "Khong_ma_KH")
    sequence = safe_segment(record.get(sequence_field), str(image_index))
    digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:10]
    prefix = f"{customer}_{image_date:%Y%m%d}_{sequence}_{digest}"
    return employee, customer, prefix


@dataclass
class SharePointImageCatalog:
    client: Any
    drive_id: str
    root_folder: str = "Data anh"
    _folder_cache: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def _children(self, remote_folder: str) -> list[dict[str, Any]]:
        if remote_folder not in self._folder_cache:
            self._folder_cache[remote_folder] = self.client.list_folder_children(
                self.drive_id, remote_folder
            )
        return self._folder_cache[remote_folder]

    def resolve_path(
        self,
        record: dict[str, Any],
        image_url: str,
        image_date: date,
        image_index: int,
    ) -> str | None:
        employee, customer, prefix = image_prefix(
            record, image_url, image_date, image_index
        )
        folder = f"{self.root_folder}/{image_date:%Y-%m}/{employee}/{customer}"
        try:
            children = self._children(folder)
        except Exception:
            return None
        matches = sorted(
            str(item.get("name", ""))
            for item in children
            if "folder" not in item and str(item.get("name", "")).startswith(prefix)
        )
        if not matches:
            return None
        return f"{folder}/{matches[0]}"
