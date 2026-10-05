from __future__ import annotations

import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from math import ceil

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from promotion_bonus import write_workbook

LAYOUT = Path(__file__).resolve().parents[1] / "config/promotion_detail_layout.json"


def write_detail_workbook(frames, filename: str, month: date, output_dir: Path = Path("output")) -> Path:
    """Production workbook writer: apply the user's sanitized template without customer data."""
    layout = json.loads(LAYOUT.read_text(encoding="utf-8"))
    if len(frames["BaoCao"]) + 4 > 1_048_576:
        raise ValueError("Promotion detail exceeds Excel row limit with template header")
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / filename
    with tempfile.TemporaryDirectory(dir=output_dir) as staging:
        path = write_workbook(frames, filename, Path(staging))
        workbook = load_workbook(path)
        try:
            sheet = workbook["BaoCao"]
            sheet.insert_rows(1, 3)
            sheet["A1"] = "Thời gian in:"
            sheet["B1"] = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%d/%m/%Y %H:%M:%S")
            sheet["A2"] = "Tháng báo cáo:"
            sheet["B2"] = month.strftime("%Y-%m")
            sheet["A3"] = layout["title"]
            sheet["Y3"] = "(Đơn vị: KÉT/THÙNG/BÌNH; vật phẩm tặng: CÁI)"
            for merged in layout["merged"]:
                sheet.merge_cells(merged)
            for column, (label, width) in enumerate(zip(layout["headers"], layout["widths"], strict=True), 1):
                cell = sheet.cell(4, column, label)
                cell.font = Font(name="Times New Roman", size=10, bold=True)
                cell.fill = PatternFill("solid", fgColor="CCFF99")
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                sheet.column_dimensions[get_column_letter(column)].width = width
            sheet["A3"].font = Font(name="Times New Roman", size=14, bold=True)
            sheet["A3"].alignment = Alignment(horizontal="center", vertical="center")
            sheet["Y3"].alignment = Alignment(horizontal="center", wrap_text=True)
            sheet.row_dimensions[3].height = 30
            sheet.row_dimensions[4].height = 36
            body_font = Font(name="Times New Roman", size=10)
            wrapped = Alignment(vertical="center", wrap_text=True)
            for row in sheet.iter_rows(min_row=5):
                lines = 1
                for cell in row:
                    cell.font = body_font
                    if cell.column in {10, 12, 13, 21, 25}:
                        cell.alignment = wrapped
                        width = max(layout["widths"][cell.column - 1] - 2, 1)
                        lines = max(lines, sum(max(ceil(len(part) / width), 1) for part in str(cell.value or "").split("\n")))
                    if cell.column in {22, 23, 26}:
                        if cell.column == 23:
                            cell.number_format = "#,##0.00"
                        else:
                            integer = isinstance(cell.value, (int, float)) and float(cell.value).is_integer()
                            cell.number_format = "#,##0" if integer else "#,##0.######"
                    if cell.column == 16:
                        cell.number_format = "dd/mm/yyyy"
                sheet.row_dimensions[row[0].row].height = min(400, 15 * lines)
            sheet.freeze_panes = "A5"
            sheet.auto_filter.ref = f"A4:Z{sheet.max_row}"
            sheet.print_title_rows = "1:4"
            sheet.print_options.horizontalCentered = True
            sheet.page_setup.orientation = "landscape"
            sheet.page_setup.fitToWidth = 1
            sheet.page_setup.fitToHeight = 0
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.print_area = f"A1:Z{sheet.max_row}"
            workbook.save(path)
        finally:
            workbook.close()
        path.replace(destination)
    return destination
