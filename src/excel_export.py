from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd


def export_excel(records: list[dict[str, Any]], report_name: str, target_date: date) -> Path:
    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{report_name}_{target_date:%Y-%m-%d}.xlsx"

    frame = pd.json_normalize(records, sep="_") if records else pd.DataFrame()
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Data", index=False)
        worksheet = writer.book["Data"]
        worksheet.freeze_panes = "A2"
        if worksheet.max_row >= 1 and worksheet.max_column >= 1:
            worksheet.auto_filter.ref = worksheet.dimensions

    return path
