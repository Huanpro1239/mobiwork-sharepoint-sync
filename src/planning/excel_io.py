from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

import openpyxl


def column_letters(start: int, end: int) -> list[str]:
    from openpyxl.utils import get_column_letter
    return [get_column_letter(i) for i in range(start, end + 1)]


def read_sheet_rows(
    workbook_bytes: bytes,
    sheet_name: str,
    *,
    min_row: int,
    max_col: int,
    data_only: bool = True,
) -> list[dict[str, Any]]:
    wb = openpyxl.load_workbook(BytesIO(workbook_bytes), read_only=True, data_only=data_only, keep_vba=True)
    if sheet_name not in wb.sheetnames:
        raise KeyError(f"Sheet {sheet_name!r} not found. Available: {wb.sheetnames}")
    ws = wb[sheet_name]
    letters = column_letters(1, max_col)
    rows: list[dict[str, Any]] = []
    for values in ws.iter_rows(min_row=min_row, max_col=max_col, values_only=True):
        if not any(value not in (None, "") for value in values):
            continue
        rows.append({letters[i]: values[i] for i in range(len(values))})
    return rows


def read_table_by_header(
    workbook_bytes: bytes,
    sheet_name: str,
    *,
    header_row: int,
    min_row: int | None = None,
    data_only: bool = True,
) -> list[dict[str, Any]]:
    wb = openpyxl.load_workbook(BytesIO(workbook_bytes), read_only=True, data_only=data_only, keep_vba=True)
    ws = wb[sheet_name]
    headers = [cell.value for cell in ws[header_row]]
    last = max((i for i, value in enumerate(headers, start=1) if value not in (None, "")), default=0)
    names = [str(value).strip() if value is not None else f"_col_{i}" for i, value in enumerate(headers[:last], start=1)]
    rows: list[dict[str, Any]] = []
    for values in ws.iter_rows(min_row=min_row or header_row + 1, max_col=last, values_only=True):
        if not any(value not in (None, "") for value in values):
            continue
        rows.append(dict(zip(names, values, strict=False)))
    return rows


def write_shadow_workbook(tables: dict[str, list[dict[str, Any]]], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for raw_name, rows in tables.items():
        name = raw_name[:31]
        ws = wb.create_sheet(name)
        if not rows:
            ws.append(["No data"])
            continue
        headers = list(rows[0].keys())
        ws.append(headers)
        for row in rows:
            ws.append([row.get(header) for header in headers])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.font = openpyxl.styles.Font(bold=True)
    wb.save(output)
    return output
