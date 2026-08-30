"""Pure Excel-reader helpers for KPI source workbooks."""
from __future__ import annotations

from io import BytesIO

import pandas as pd


def frame_from_excel(content: bytes, remote_path: str, export_mode: str) -> pd.DataFrame:
    if not content:
        raise ValueError(f"SharePoint workbook is empty: {remote_path}")
    sheet_name = "ChiTietSP" if export_mode == "order" else "Data"
    frame = pd.read_excel(BytesIO(content), sheet_name=sheet_name, engine="openpyxl")
    return frame.astype(object).where(pd.notna(frame), None)
