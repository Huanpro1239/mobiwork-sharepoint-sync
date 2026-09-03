from __future__ import annotations

import re
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl.utils import get_column_letter

from excel_export import _assert_unique, _format_sheet, _validate_excel_size, build_order_frames


SYNC_DATE_COLUMN = "_sync_date"


def master_filename(report_name: str, target_date: date) -> str:
    return f"{report_name}_{target_date:%Y-%m}.xlsx"


def month_dates_through(target_date: date) -> list[date]:
    first = target_date.replace(day=1)
    return [first + timedelta(days=offset) for offset in range((target_date - first).days + 1)]


def _empty_frames(export_mode: str) -> dict[str, pd.DataFrame]:
    if export_mode == "order":
        return {"DonHang": pd.DataFrame(), "ChiTietSP": pd.DataFrame()}
    return {"Data": pd.DataFrame()}


def frames_from_records(
    records: list[dict[str, Any]],
    export_mode: str,
    target_date: date,
) -> dict[str, pd.DataFrame]:
    partition = target_date.isoformat()
    if export_mode == "order":
        header, detail = build_order_frames(records)
        header.insert(0, SYNC_DATE_COLUMN, partition)
        detail.insert(0, SYNC_DATE_COLUMN, partition)
        return {"DonHang": header, "ChiTietSP": detail}

    frame = pd.json_normalize(records, sep="_") if records else pd.DataFrame()
    frame.insert(0, SYNC_DATE_COLUMN, partition)
    _validate_excel_size(frame, "Data")
    return {"Data": frame}


def read_master(content: bytes, export_mode: str) -> dict[str, pd.DataFrame]:
    if not content:
        return _empty_frames(export_mode)

    source = BytesIO(content)
    if export_mode == "order":
        frames = {
            "DonHang": pd.read_excel(source, sheet_name="DonHang", engine="openpyxl"),
        }
        source.seek(0)
        frames["ChiTietSP"] = pd.read_excel(
            source,
            sheet_name="ChiTietSP",
            engine="openpyxl",
        )
    else:
        frames = {"Data": pd.read_excel(source, sheet_name="Data", engine="openpyxl")}

    for sheet_name, frame in frames.items():
        if SYNC_DATE_COLUMN not in frame.columns:
            raise ValueError(
                f"Monthly master sheet {sheet_name!r} is missing {SYNC_DATE_COLUMN}; "
                "a one-time month rebuild is required"
            )
        frame[SYNC_DATE_COLUMN] = frame[SYNC_DATE_COLUMN].astype("string")
    return frames


def _combine_partition_frames(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Combine two partitions without passing empty frames through pandas concat."""
    if old.empty:
        return new.reset_index(drop=True).copy()
    if new.empty:
        return old.reset_index(drop=True).copy()
    return pd.concat([old, new], ignore_index=True, sort=False)


def merge_partition(
    existing: dict[str, pd.DataFrame],
    incoming: dict[str, pd.DataFrame],
    target_date: date,
    export_mode: str,
) -> dict[str, pd.DataFrame]:
    partition = target_date.isoformat()
    sheet_names = ("DonHang", "ChiTietSP") if export_mode == "order" else ("Data",)
    merged: dict[str, pd.DataFrame] = {}

    incoming_order_ids: set[str] = set()
    if export_mode == "order":
        header_new = incoming.get("DonHang", pd.DataFrame())
        if not header_new.empty and "ma_phieu" in header_new.columns:
            incoming_order_ids = set(header_new["ma_phieu"].astype("string").dropna().unique())

    incoming_customer_ids: set[str] = set()
    if export_mode == "flat":
        data_new = incoming.get("Data", pd.DataFrame())
        if not data_new.empty and "makh" in data_new.columns and "ma_nv" not in data_new.columns:
            incoming_customer_ids = set(data_new["makh"].astype("string").dropna().unique())

    for sheet_name in sheet_names:
        old = existing.get(sheet_name, pd.DataFrame()).copy()
        new = incoming.get(sheet_name, pd.DataFrame()).copy()
        if not old.empty:
            if SYNC_DATE_COLUMN not in old.columns:
                raise ValueError(
                    f"Monthly master sheet {sheet_name!r} is missing {SYNC_DATE_COLUMN}"
                )
            mask = old[SYNC_DATE_COLUMN].astype("string") != partition
            if incoming_order_ids and "ma_phieu" in old.columns:
                mask = mask & (~old["ma_phieu"].astype("string").isin(incoming_order_ids))
            if incoming_customer_ids and "makh" in old.columns:
                mask = mask & (~old["makh"].astype("string").isin(incoming_customer_ids))
            old = old.loc[mask]
        combined = _combine_partition_frames(old, new)
        if SYNC_DATE_COLUMN in combined.columns:
            combined[SYNC_DATE_COLUMN] = combined[SYNC_DATE_COLUMN].astype("string")
            combined = combined.sort_values(SYNC_DATE_COLUMN, kind="stable").reset_index(drop=True)
        _validate_excel_size(combined, sheet_name)
        merged[sheet_name] = combined

    if export_mode == "order":
        _assert_unique(merged["DonHang"], ["ma_phieu"], "DonHang monthly master")
        _assert_unique(
            merged["ChiTietSP"],
            ["ma_phieu", "stt"],
            "ChiTietSP monthly master",
        )
    return merged


def build_month_from_partitions(
    partitions: list[tuple[date, list[dict[str, Any]]]],
    export_mode: str,
) -> dict[str, pd.DataFrame]:
    master = _empty_frames(export_mode)
    for target_date, records in partitions:
        incoming = frames_from_records(records, export_mode, target_date)
        master = merge_partition(master, incoming, target_date, export_mode)
    return master


def write_master(
    frames: dict[str, pd.DataFrame],
    report_name: str,
    target_date: date,
) -> Path:
    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / master_filename(report_name, target_date)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet_name, frame in frames.items():
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
            _format_sheet(writer, sheet_name)
            worksheet = writer.book[sheet_name]
            headers = {
                worksheet.cell(row=1, column=column).value: column
                for column in range(1, worksheet.max_column + 1)
            }
            sync_column = headers.get(SYNC_DATE_COLUMN)
            if sync_column:
                worksheet.column_dimensions[get_column_letter(sync_column)].hidden = True
    return path


def master_row_count(frames: dict[str, pd.DataFrame], export_mode: str) -> int:
    if export_mode == "order":
        return len(frames.get("DonHang", pd.DataFrame()))
    return len(frames.get("Data", pd.DataFrame()))


def is_legacy_report_file(name: str, report_name: str, canonical_name: str) -> bool:
    if name == canonical_name:
        return False
    if name.startswith(("__sync_tmp_", "__sync_backup_", "__sync_failed_")):
        return True
    escaped = re.escape(report_name)
    if re.fullmatch(rf"{escaped}_\d{{4}}-\d{{2}}-\d{{2}}\.xlsx", name):
        return True
    return bool(re.fullmatch(rf"{escaped}_History_.*\.xlsx", name, flags=re.IGNORECASE))
