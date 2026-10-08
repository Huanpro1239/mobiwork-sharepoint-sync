from __future__ import annotations

import re
from datetime import date
from typing import Any

import pandas as pd

from data_cham_anh_export import _monthly_master_path


def parse_requested_months(raw: str, today: date) -> list[date]:
    """Validate explicit calendar months before any historical read or write."""
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        raise ValueError("Requested months must list YYYY-MM months")
    anchors = set()
    for part in parts:
        if not re.fullmatch(r"\d{4}-\d{2}", part):
            raise ValueError(f"Invalid requested month: {part}; use YYYY-MM")
        year, month = map(int, part.split("-"))
        anchor = date(year, month, 1)
        if anchor > today.replace(day=1):
            raise ValueError(f"Requested month is in the future: {part}")
        anchors.add(anchor)
    return sorted(anchors)


def discover_bill_months(sharepoint: Any, drive: str, bill: Any, today: date) -> list[date]:
    """Find canonical monthly masters, including completed years, without probing guessed dates."""
    anchors = []
    for year in sharepoint.list_folder_children(drive, bill.folder):
        name = str(year.get("name", ""))
        if "folder" not in year or not re.fullmatch(r"\d{4}", name):
            continue
        for month in sharepoint.list_folder_children(drive, f"{bill.folder}/{name}"):
            month_name = str(month.get("name", ""))
            if "folder" not in month or not re.fullmatch(r"\d{2}", month_name):
                continue
            anchor = date(int(name), int(month_name), 1)
            if anchor > today:
                continue
            path = _monthly_master_path(bill, anchor)
            item = sharepoint.get_item_by_path(drive, path)
            if not item or "file" not in item:
                raise ValueError(f"Bill month folder exists but canonical master is missing: {path}")
            anchors.append(anchor)
    if not anchors:
        raise ValueError("No historical Bill monthly masters found on SharePoint")
    if len(anchors) > 120:
        raise ValueError("Promotion history exceeds 120 months; choose an explicit range")
    return sorted(set(anchors))


def select_order_month(detail: pd.DataFrame, anchor: date) -> tuple[pd.DataFrame, int]:
    """Keep invalid/missing dates for blocking validation, never silently drop them."""
    def belongs(row):
        raw = row.get("ngay_dat")
        if raw is None or pd.isna(raw) or not str(raw).strip():
            raw = row.get("ngay_ban_hang")
        try:
            parsed = pd.Timestamp(raw)
            if pd.isna(parsed) or parsed.tzinfo is not None:
                return True
            return (parsed.year, parsed.month) == (anchor.year, anchor.month)
        except (TypeError, ValueError):
            return True
    mask = detail.apply(belongs, axis=1) if not detail.empty else pd.Series([], dtype=bool)
    return detail.loc[mask].copy(), int((~mask).sum())
