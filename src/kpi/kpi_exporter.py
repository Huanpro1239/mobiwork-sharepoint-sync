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

from kpi.manual_labels import labels_from_sheet, load_manual_labels, safe_text
from kpi.output_contract import validate_workbook, validate_workbook_file
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
    15: "Mở File Cục Bộ",
    16: "Kết Quả Khách Hàng",
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
    """Validate only the immutable parts needed to safely transform the template."""

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
    if not any(value.startswith("1. DANH SÁCH DÒNG ĐƠN HÀNG") for value in alert_values):
        raise ValueError(f"Workbook mẫu thiếu mục 1 trong {ALERT_SHEET}")
    if not any(value.startswith("2. DANH SÁCH ẢNH") for value in alert_values):
        raise ValueError(f"Workbook mẫu thiếu vùng cảnh báo ảnh trong {ALERT_SHEET}")
    if not any(value.startswith("3. BÁO CÁO PHÂN BỔ") for value in alert_values):
        raise ValueError(f"Workbook mẫu thiếu mục 3 trong {ALERT_SHEET}")


def _copy_style(source, target) -> None:
    if source.has_style:
        target._style = copy(source._style)
    target.number_format = source.number_format
    target.alignment = copy(source.alignment)
    target.protection = copy(source.protection)


def _period_label(period: pd.Timestamp) -> str:
    return f"Tháng {period:%m/%Y}"


def _apply_titles(workbook, period: pd.Timestamp) -> None:
    workbook[SUMMARY_SHEET]["A1"] = "BẢNG TỔNG HỢP CHẤM CÔNG & TÍNH TIỀN THƯỞNG KPI (CÔNG THỨC SỐNG)"
    workbook[SUMMARY_SHEET]["A2"] = (
        f"Áp dụng: {_period_label(period)} | Đơn vị: Công ty Cổ phần Nước khoáng Khánh Hòa | "
        "Mọi số liệu tự động nhảy theo sheet Chi_tiet_Khach_hang & Chi_tiet_Anh_Checkin"
    )
    workbook[CUSTOMER_SHEET]["A1"] = "DANH SÁCH CHI TIẾT ĐÁNH GIÁ TỪNG KHÁCH HÀNG (CÔNG THỨC SỐNG)"
    workbook[CUSTOMER_SHEET]["A2"] = (
        "Quy trình 4 bước: B1 Phân loại KH | B2 Đơn hàng (>= 3 KTB) | B3 Ghi tồn | "
        "B4 Chấm ảnh (KH mới: Biển hiệu + Trưng bày) | Sửa nhãn bên sheet Chi_tiet_Anh_Checkin tự nhảy kết quả"
    )
    workbook[DETAIL_SHEET]["A1"] = "BẢNG NHẬT KÝ ẢNH CHECK-IN ĐÃ PHÂN LOẠI (HỖ TRỢ SỬA TAY)"
    workbook[DETAIL_SHEET]["A2"] = (
        "Gõ nhãn vào cột 'Nhãn Sửa Tay' (ô màu vàng nhạt) để đổi nhãn -> "
        "Toàn bộ bảng công và thưởng sẽ tự động tính lại"
    )
    workbook[ALERT_SHEET]["A1"] = "BẢNG CẢNH BÁO RỦI RO & BẤT THƯỜNG DỮ LIỆU KPI"
    workbook[ALERT_SHEET]["A2"] = (
        f"{_period_label(period)} | Liệt kê các dòng thiếu đơn vị tính, khách phụ thuộc ảnh biên và bất thường doanh số"
    )


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
        self.last_validation: dict[str, object] | None = None

    def export_full_workbook(
        self,
        df_results: pd.DataFrame,
        customer_frame: pd.DataFrame,
        period_start,
        kpi_warnings: tuple[str, ...] = (),
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
            period = pd.Timestamp(period_start).normalize()
            _apply_titles(workbook, period)
            write_parameters(workbook[PARAM_SHEET], period, kpi_warnings)
            customer_end = replace_customer_rows(workbook[CUSTOMER_SHEET], customer_frame)
            self._replace_detail_rows(
                workbook[DETAIL_SHEET], df_results, manual_labels, customer_end
            )
            update_customer_image_formulas(
                workbook[CUSTOMER_SHEET], len(df_results), df_results
            )
            replace_summary_rows(workbook[SUMMARY_SHEET], customer_frame, customer_end)
            self._update_review_alerts(workbook[ALERT_SHEET], df_results)
            workbook.calculation.calcMode = "auto"
            workbook.calculation.fullCalcOnLoad = True
            workbook.calculation.forceFullCalc = True
            # Validate in memory before touching even the temporary output.
            self.last_validation = validate_workbook(
                workbook,
                expected_customers=len(customer_frame),
                expected_images=len(df_results),
            )
            workbook.save(temporary)
        finally:
            workbook.close()

        # Re-open the serialized XLSX. This catches broken relationships/formulas
        # introduced during save before production SharePoint can be overwritten.
        self.last_validation = validate_workbook_file(
            temporary,
            expected_customers=len(customer_frame),
            expected_images=len(df_results),
        )
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
        for column, header in DETAIL_HEADERS.items():
            sheet.cell(4, column, header)._style = copy(header_style)
        for column, header, _ in AUDIT_COLUMNS:
            sheet.cell(4, column, header)._style = copy(header_style)

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
        # Keep the operational audit columns J:M hidden as in the approved file.
        for column in ("J", "K", "L", "M"):
            sheet.column_dimensions[column].hidden = True
        sheet.freeze_panes = "A5"
        sheet.auto_filter.ref = f"A4:{openpyxl.utils.get_column_letter(max_column)}{end_row}"

    @staticmethod
    def _update_review_alerts(sheet, frame: pd.DataFrame) -> None:
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
        header_row = section_row + 1
        data_row = header_row + 1
        if next_section > data_row:
            sheet.delete_rows(data_row, next_section - data_row)
        review = (
            frame[frame["Phân Loại AI"].isin(("Can_duyet", "Khong_the_cham"))]
            if "Phân Loại AI" in frame.columns
            else frame.iloc[0:0]
        )
        sheet.insert_rows(data_row, amount=max(1, len(review)))
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
        for col, header in enumerate(headers, start=1):
            sheet.cell(header_row, col, header)
        if review.empty:
            sheet.cell(data_row, 1, "Không có ảnh cần duyệt hoặc lỗi kỹ thuật.")
            return
        for offset, record in enumerate(review.to_dict(orient="records")):
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
                sheet.cell(data_row + offset, col, value)
