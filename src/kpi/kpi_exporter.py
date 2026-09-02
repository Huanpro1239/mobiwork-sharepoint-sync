"""Template-preserving KPI workbook exporter with live image formulas."""
from __future__ import annotations

import os
from copy import copy
from pathlib import Path

import openpyxl
import pandas as pd
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from rich.console import Console

from kpi.manual_labels import (
    ManualLabelIndex,
    labels_from_sheet,
    load_manual_labels,
    safe_text,
)
from kpi.review_queue import partition_review_rows, summarize_review_rows
from kpi.workbook_formulas import (
    START_ROW,
    replace_customer_rows,
    replace_summary_rows,
    update_customer_image_formulas,
    write_parameters,
)
from project_paths import OUTPUT_EXCEL, TEMPLATE_EXCEL

console = Console()
SUMMARY_SHEET = "Tong_hop_KPI_Nhan_vien"
CUSTOMER_SHEET = "Chi_tiet_Khach_hang"
DETAIL_SHEET = "Chi_tiet_Anh_Checkin"
ALERT_SHEET = "Canh_bao"
PARAM_SHEET = "Tham_so"
REQUIRED_SHEETS = (SUMMARY_SHEET, CUSTOMER_SHEET, DETAIL_SHEET, ALERT_SHEET, PARAM_SHEET)

DETAIL_HEADERS = {
    1: "STT",
    2: "Tên Nhân Viên",
    3: "Ngày",
    4: "Mã Khách Hàng",
    5: "Tên Khách Hàng",
    6: "STT Hình",
    7: "Phân Loại AI",
    8: "Nhãn Sửa Tay (Gõ đè vào đây)",
    9: "Nhãn Dùng Thực Tế",
    10: "Độ Tin Cậy AI",
    11: "Tên File",
    12: "Căn Cứ Nhận Diện",
    13: "Nội Dung Chữ OCR",
    14: "Mở Ảnh DMS",
}

AUDIT_COLUMNS = (
    (17, "Trạng Thái Quyết Định", "Trạng Thái Quyết Định"),
    (18, "Loại Cảnh", "Loại Cảnh"),
    (19, "Điểm Scene", "Điểm Scene"),
    (20, "Điểm Pass", "Điểm Pass"),
    (21, "Điểm Fraud", "Điểm Fraud"),
    (22, "Độ Tương Đồng Mẫu", "Độ Tương Đồng Mẫu"),
    (23, "3 Tham Chiếu Gần Nhất", "3 Tham Chiếu Gần Nhất"),
    (24, "Bằng Chứng Detector", "Bằng Chứng Detector"),
    (25, "Quality Gate", "Quality Gate"),
    (26, "Pipeline Signature", "pipeline_signature"),
    (27, "Record ID", "record_id"),
    (28, "Ghi Chú Nguồn", "ghi_chu"),
    (29, "Source Index", "_source_index"),
    (30, "Ảnh SHA256", "image_sha256"),
)


def _validate_template(workbook) -> None:
    if tuple(workbook.sheetnames) != REQUIRED_SHEETS:
        raise ValueError(
            f"Workbook mẫu không đúng hợp đồng 5 sheet: cần {REQUIRED_SHEETS}, "
            f"nhận {tuple(workbook.sheetnames)}"
        )
    details = workbook[DETAIL_SHEET]
    for column, expected in DETAIL_HEADERS.items():
        actual = safe_text(details.cell(4, column).value).strip()
        if actual != expected:
            coordinate = details.cell(4, column).coordinate
            raise ValueError(
                f"Workbook mẫu sai header {DETAIL_SHEET}!{coordinate}: "
                f"{actual!r}; cần {expected!r}"
            )
    alert_values = [
        safe_text(workbook[ALERT_SHEET].cell(row, 1).value).strip()
        for row in range(1, workbook[ALERT_SHEET].max_row + 1)
    ]
    if not any(value.startswith("2. DANH SÁCH ẢNH") for value in alert_values):
        raise ValueError(f"Workbook mẫu thiếu vùng cảnh báo ảnh trong {ALERT_SHEET}")
    if not any(value.startswith("3. ") for value in alert_values):
        raise ValueError(f"Workbook mẫu thiếu mục 3 trong {ALERT_SHEET}")


def _copy_style(source, target) -> None:
    if source.has_style:
        target._style = copy(source._style)
    target.number_format = source.number_format
    target.alignment = copy(source.alignment)
    target.protection = copy(source.protection)


class KPIExporter:
    def __init__(
        self,
        template_path=TEMPLATE_EXCEL,
        output_path=OUTPUT_EXCEL,
        manual_label_source_path=None,
        announce: bool = True,
    ) -> None:
        self.template_path = Path(template_path)
        self.output_path = Path(output_path)
        self.manual_label_source_path = Path(
            manual_label_source_path if manual_label_source_path is not None else output_path
        )
        self.announce = announce
        self.review_summary: dict[str, object] = {}

    def export_full_workbook(
        self,
        df_results: pd.DataFrame,
        customer_frame: pd.DataFrame,
        period_start,
        kpi_warnings: tuple[str, ...] = (),
        current_pipeline_signature: str = "",
    ) -> Path:
        if not self.template_path.is_file():
            raise FileNotFoundError(f"Không tìm thấy workbook mẫu: {self.template_path}")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        prior_labels = load_manual_labels(self.manual_label_source_path)
        workbook = openpyxl.load_workbook(self.template_path)
        temporary = self.output_path.with_name(
            f"{self.output_path.stem}_atomic{self.output_path.suffix}"
        )
        try:
            _validate_template(workbook)
            template_labels = labels_from_sheet(workbook[DETAIL_SHEET], self.template_path)
            manual_labels = template_labels.overlay(prior_labels)
            self.review_summary = summarize_review_rows(
                df_results,
                manual_labels,
                current_pipeline_signature=current_pipeline_signature,
            )
            warning_values = tuple(
                dict.fromkeys((*kpi_warnings, *self.review_summary.get("warnings", [])))
            )
            period = pd.Timestamp(period_start).normalize()
            write_parameters(workbook[PARAM_SHEET], period, warning_values)
            customer_end = replace_customer_rows(workbook[CUSTOMER_SHEET], customer_frame)
            self._replace_detail_rows(
                workbook[DETAIL_SHEET], df_results, manual_labels, customer_end
            )
            update_customer_image_formulas(workbook[CUSTOMER_SHEET], len(df_results))
            replace_summary_rows(workbook[SUMMARY_SHEET], customer_frame, customer_end)
            self._update_review_alerts(
                workbook[ALERT_SHEET], df_results, manual_labels
            )
            workbook.calculation.calcMode = "auto"
            workbook.calculation.fullCalcOnLoad = True
            workbook.calculation.forceFullCalc = True
            workbook.save(temporary)
        finally:
            workbook.close()
        os.replace(temporary, self.output_path)
        if self.announce:
            console.print(f"[bold green]Workbook KPI đã xuất: {self.output_path}[/bold green]")
        return self.output_path

    def _replace_detail_rows(self, sheet, frame, manual_labels, customer_end_row: int) -> None:
        max_column = AUDIT_COLUMNS[-1][0]
        style_row = START_ROW if sheet.max_row >= START_ROW else 4
        source_styles = [copy(sheet.cell(style_row, col)._style) for col in range(1, 17)]
        source_formats = [sheet.cell(style_row, col).number_format for col in range(1, 17)]
        header_style = copy(sheet.cell(4, min(14, sheet.max_column))._style)
        if sheet.max_row >= START_ROW:
            sheet.delete_rows(START_ROW, sheet.max_row - START_ROW + 1)
        for column, header, _ in AUDIT_COLUMNS:
            sheet.cell(4, column, header)._style = copy(header_style)
        if sheet.cell(4, 16).value is None:
            sheet.cell(4, 16, "Kết Quả Khách Hàng")._style = copy(header_style)

        records = frame.reset_index(drop=True).to_dict(orient="records")
        lookup_end = max(START_ROW, customer_end_row)
        for offset, record in enumerate(records):
            row = START_ROW + offset
            for col in range(1, 17):
                sheet.cell(row, col)._style = copy(source_styles[col - 1])
                sheet.cell(row, col).number_format = source_formats[col - 1]
            url = safe_text(record.get("hinh_anh"))
            values = {
                1: offset + 1,
                2: record.get("ten_nhan_vien", ""),
                3: record.get("ngay", ""),
                4: record.get("ma_kh", ""),
                5: record.get("ten_kh", ""),
                6: record.get("stt_hinh", ""),
                7: record.get("Phân Loại AI", ""),
                8: manual_labels.lookup(record),
                9: f'=IF(H{row}<>"",H{row},G{row})',
                10: record.get("Độ Tin Cậy AI"),
                11: record.get("Tên File", ""),
                12: record.get("Căn Cứ Nhận Diện", ""),
                13: record.get("Nội Dung Chữ OCR", ""),
                14: "Mở DMS" if url.casefold().startswith(("http://", "https://")) else "",
                15: "",
                16: (
                    f'=IFERROR(VLOOKUP(D{row},\'{CUSTOMER_SHEET}\'!$C$4:$P${lookup_end},14,0),0)'
                ),
            }
            for col, value in values.items():
                sheet.cell(row, col, value)
            if values[14]:
                sheet.cell(row, 14).hyperlink = url
                sheet.cell(row, 14).style = "Hyperlink"
            for column, _, key in AUDIT_COLUMNS:
                target = sheet.cell(row, column, record.get(key, ""))
                reference = sheet.cell(row, 12 if column in (23, 24, 26, 27, 28, 30) else 10)
                _copy_style(reference, target)

        end_row = max(START_ROW, START_ROW + len(records) - 1)
        validation = DataValidation(
            type="list", formula1='"Bien_hieu,Trung_bay,Khong_dat"', allow_blank=True
        )
        validation.error = "Chọn một trong ba nhãn KPI chuẩn."
        validation.errorTitle = "Nhãn không hợp lệ"
        validation.prompt = "Chỉ sửa khi đã duyệt ảnh thủ công."
        validation.promptTitle = "Nhãn Sửa Tay"
        sheet.add_data_validation(validation)
        validation.add(f"H{START_ROW}:H{end_row}")
        if records:
            sheet.conditional_formatting.add(
                f"G{START_ROW}:G{end_row}",
                FormulaRule(
                    formula=[f'G{START_ROW}="Can_duyet"'],
                    fill=PatternFill("solid", fgColor="FFF2CC"),
                ),
            )
            sheet.conditional_formatting.add(
                f"G{START_ROW}:G{end_row}",
                FormulaRule(
                    formula=[f'G{START_ROW}="Khong_the_cham"'],
                    fill=PatternFill("solid", fgColor="F4CCCC"),
                ),
            )
        sheet.freeze_panes = "A5"
        sheet.auto_filter.ref = f"A4:{openpyxl.utils.get_column_letter(max_column)}{end_row}"

    @staticmethod
    def _update_review_alerts(
        sheet,
        frame: pd.DataFrame,
        manual_labels: ManualLabelIndex,
    ) -> None:
        section_row = next(
            (
                row
                for row in range(1, sheet.max_row + 1)
                if safe_text(sheet.cell(row, 1).value).startswith("2. DANH SÁCH ẢNH")
            ),
            None,
        )
        next_section = next(
            (
                row
                for row in range((section_row or 0) + 1, sheet.max_row + 1)
                if safe_text(sheet.cell(row, 1).value).startswith("3. ")
            ),
            None,
        )
        if section_row is None or next_section is None:
            raise ValueError(f"Không tìm thấy đầy đủ mục 2/3 trong sheet {ALERT_SHEET}")
        heading_style = [copy(sheet.cell(section_row, col)._style) for col in range(1, 10)]
        header_source_row = min(section_row + 1, sheet.max_row)
        header_style = [
            copy(sheet.cell(header_source_row, col)._style) for col in range(1, 10)
        ]
        data_source_row = min(section_row + 2, sheet.max_row)
        data_style = [
            copy(sheet.cell(data_source_row, col)._style) for col in range(1, 10)
        ]
        if next_section > section_row + 1:
            sheet.delete_rows(section_row + 1, next_section - section_row - 1)

        partitions = partition_review_rows(frame, manual_labels)
        pending_limit = max(
            1,
            int(os.environ.get("KPI_PENDING_ALERT_SAMPLE_LIMIT", "100")),
        )
        sections = (
            (
                "2A. ẢNH THẬT SỰ CẦN DUYỆT TAY "
                f"({len(partitions.manual_required):,})",
                partitions.manual_required,
                "Không có ảnh nào đang chờ người duyệt.",
            ),
            (
                "2B. LỖI KỸ THUẬT — KHÔNG PHẢI DUYỆT NHÃN "
                f"({len(partitions.technical):,})",
                partitions.technical,
                "Không có lỗi kỹ thuật bị chặn.",
            ),
            (
                "2C. CHỜ CHẤM AI — KHÔNG PHẢI DUYỆT NHÃN "
                f"({len(partitions.pending):,}; hiển thị tối đa {pending_limit:,})",
                partitions.pending.head(pending_limit),
                "Không còn ảnh chờ chấm AI.",
            ),
        )
        headers = (
            "STT",
            "Tên Nhân Viên",
            "Mã KH",
            "Tên KH",
            "Nhãn AI",
            "Độ Tin Cậy",
            "Lý Do Cần Duyệt",
            "Tên File",
            "Cảnh Báo",
        )
        insert_at = section_row + 1
        total_rows = sum(2 + max(1, len(data)) for _, data, _ in sections)
        sheet.insert_rows(insert_at, amount=total_rows)

        cursor = insert_at
        for title, data, empty_message in sections:
            for col in range(1, 10):
                sheet.cell(cursor, col)._style = copy(heading_style[col - 1])
            sheet.cell(cursor, 1, title)
            cursor += 1

            for col, header in enumerate(headers, start=1):
                target = sheet.cell(cursor, col, header)
                target._style = copy(header_style[col - 1])
            cursor += 1

            if data.empty:
                for col in range(1, 10):
                    sheet.cell(cursor, col)._style = copy(data_style[col - 1])
                sheet.cell(cursor, 1, empty_message)
                cursor += 1
                continue

            for offset, record in enumerate(data.to_dict(orient="records")):
                row = cursor + offset
                values = (
                    offset + 1,
                    record.get("ten_nhan_vien", ""),
                    record.get("ma_kh", ""),
                    record.get("ten_kh", ""),
                    record.get("Phân Loại AI", ""),
                    record.get("Độ Tin Cậy AI"),
                    record.get("Căn Cứ Nhận Diện", ""),
                    record.get("Tên File", ""),
                    record.get("Trạng Thái Quyết Định", ""),
                )
                for col, value in enumerate(values, start=1):
                    target = sheet.cell(row, col, value)
                    target._style = copy(data_style[col - 1])
            cursor += len(data)
