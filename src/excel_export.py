from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd


def _format_sheet(writer: pd.ExcelWriter, sheet_name: str) -> None:
    worksheet = writer.book[sheet_name]
    worksheet.freeze_panes = "A2"
    if worksheet.max_row >= 1 and worksheet.max_column >= 1:
        worksheet.auto_filter.ref = worksheet.dimensions


def _order_frames(records: list[dict[str, Any]]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    header_rows: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    promo_rows: list[dict[str, Any]] = []

    nested_fields = {"san_pham", "san_pham_km", "promotion"}

    for order in records:
        header = {key: value for key, value in order.items() if key not in nested_fields}
        header_rows.append(header)

        for item in order.get("san_pham") or []:
            if isinstance(item, dict):
                detail_rows.append({**header, **item})

        for item in order.get("san_pham_km") or []:
            if isinstance(item, dict):
                promo_rows.append({**header, **item})

    return (
        pd.json_normalize(header_rows, sep="_") if header_rows else pd.DataFrame(),
        pd.json_normalize(detail_rows, sep="_") if detail_rows else pd.DataFrame(),
        pd.json_normalize(promo_rows, sep="_") if promo_rows else pd.DataFrame(),
    )


def export_excel(
    records: list[dict[str, Any]],
    report_name: str,
    target_date: date,
    export_mode: str = "flat",
    file_suffix: str | None = None,
) -> Path:
    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = file_suffix or f"{target_date:%Y-%m-%d}"
    path = output_dir / f"{report_name}_{suffix}.xlsx"

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        if export_mode == "order":
            header, detail, promo = _order_frames(records)
            header.to_excel(writer, sheet_name="DonHang", index=False)
            detail.to_excel(writer, sheet_name="ChiTietSP", index=False)
            promo.to_excel(writer, sheet_name="HangKhuyenMai", index=False)
            for sheet_name in ("DonHang", "ChiTietSP", "HangKhuyenMai"):
                _format_sheet(writer, sheet_name)
        else:
            frame = pd.json_normalize(records, sep="_") if records else pd.DataFrame()
            frame.to_excel(writer, sheet_name="Data", index=False)
            _format_sheet(writer, "Data")

    return path
