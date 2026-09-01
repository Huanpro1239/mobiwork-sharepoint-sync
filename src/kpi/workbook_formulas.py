"""Live Excel formulas matching the production KPI workbook contract.

The Python aggregation layer may use rolling history internally, but the workbook
presented to Sales/Planning deliberately keeps the stable five-sheet contract used
in the approved KPI file. Manual image labels remain live Excel inputs and flow
through customer qualification and employee rewards without rerunning Python.
"""
from __future__ import annotations

from copy import copy

import pandas as pd

from kpi.kpi_rules import DEFAULT_KPI_POLICY

START_ROW = 5
DETAIL_SHEET = "Chi_tiet_Anh_Checkin"
CUSTOMER_SHEET = "Chi_tiet_Khach_hang"
PARAM_SHEET = "Tham_so"

CUSTOMER_HEADERS = {
    1: "STT",
    2: "Tên Nhân Viên",
    3: "Mã Khách Hàng",
    4: "Tên Khách Hàng",
    5: "Loại Khách Hàng",
    6: "Số Đơn Hàng",
    7: "Đơn Lớn Nhất (KTB)",
    8: "Tổng Sản Lượng (KTB)",
    9: "Đạt Đơn Hàng",
    10: "Ghi Tồn",
    11: "Ảnh Biển Hiệu",
    12: "Ảnh Trưng Bày",
    13: "Ảnh Không Đạt",
    14: "BH Theo Ghi Chú",
    15: "Đạt Ảnh Check-in",
    16: "KẾT QUẢ ĐẠT",
    17: "Chi Tiết / Lý Do Đánh Giá",
    18: "Dò Ảnh",
}

SUMMARY_HEADERS = {
    1: "STT",
    2: "Tên Nhân Viên",
    3: "Tổng KH Viếng Thăm",
    4: "Tổng KH Đạt",
    5: "KH Chưa Có Đơn",
    6: "Doanh Số Không Đủ",
    7: "Không Có Ảnh BH/TB",
    8: "Ảnh BH/TB Không Đạt",
    9: "Tỷ Lệ Đạt (Benchmark 50 KH)",
    10: "Ngày Công Chuẩn",
    11: "NGÀY CÔNG ĐƯỢC TÍNH",
    12: "Số KH Mới Đạt",
    13: "Thưởng Mở Mới (VNĐ)",
    14: "Số KH Cũ Đạt",
    15: "Thưởng Chăm Sóc Cũ (VNĐ)",
    16: "TỔNG TIỀN THƯỞNG KPI (VNĐ)",
    17: "Đánh Giá / Ghi Chú",
}


def _row_styles(sheet, end_column: int) -> tuple[list, list]:
    source_row = START_ROW if sheet.max_row >= START_ROW else 4
    styles = [copy(sheet.cell(source_row, col)._style) for col in range(1, end_column + 1)]
    formats = [sheet.cell(source_row, col).number_format for col in range(1, end_column + 1)]
    return styles, formats


def _reset_table(sheet, headers: dict[int, str]) -> tuple[list, list]:
    end_column = max(headers)
    styles, formats = _row_styles(sheet, end_column)
    header_style = copy(sheet.cell(4, min(sheet.max_column, end_column))._style)
    if sheet.max_row >= START_ROW:
        sheet.delete_rows(START_ROW, sheet.max_row - START_ROW + 1)
    for column, header in headers.items():
        cell = sheet.cell(4, column, header)
        cell._style = copy(header_style)
    return styles, formats


def _working_days_excluding_sundays(period_start: pd.Timestamp) -> int:
    start = pd.Timestamp(period_start).normalize()
    end = start + pd.offsets.MonthEnd(0)
    days = pd.date_range(start, end, freq="D")
    return int((days.dayofweek != 6).sum())


def write_parameters(sheet, period_start: pd.Timestamp, warnings: tuple[str, ...]) -> None:
    """Write the exact policy block used by the approved production workbook."""
    policy = DEFAULT_KPI_POLICY
    rows = (
        (1, "Tham Số Chính Sách & Công Chuẩn", "Giá Trị"),
        (2, "Ngưỡng KHTC (đơn lớn nhất, KTB)", policy.khtc_single_order_ktb),
        (3, "Ngưỡng KHĐĐK (cộng dồn tháng, KTB)", policy.khddk_total_ktb),
        (4, "Mức thưởng / KH mới (VNĐ)", policy.new_customer_reward_vnd),
        (5, "Mức thưởng / KH cũ (VNĐ)", policy.old_customer_reward_vnd),
        (6, "Trần số khách được thưởng / loại", policy.reward_customer_cap),
        (7, "Trần tổng thưởng / nhân viên (VNĐ)", policy.reward_total_cap_vnd),
        (8, "Benchmark KH (mẫu số tỷ lệ đạt)", policy.benchmark_customers),
        (9, "Ngày công chuẩn trong tháng", _working_days_excluding_sundays(period_start)),
    )
    for row, label, value in rows:
        sheet.cell(row, 1, label)
        sheet.cell(row, 2, value)
    for row in range(10, sheet.max_row + 1):
        sheet.cell(row, 1, None)
        sheet.cell(row, 2, None)
    for cell in ("B4", "B5", "B7"):
        sheet[cell].number_format = '#,##0 "VNĐ"'
    sheet.column_dimensions["A"].width = max(sheet.column_dimensions["A"].width or 0, 38)
    sheet.column_dimensions["B"].width = max(sheet.column_dimensions["B"].width or 0, 15)


def _customer_type(record: dict[str, object]) -> str:
    first_activity = record.get("first_activity_date")
    period = record.get("period_start")
    if first_activity is None or pd.isna(first_activity) or period is None or pd.isna(period):
        return "Không rõ"
    return (
        "Khách hàng cũ"
        if pd.Timestamp(first_activity).normalize() < pd.Timestamp(period).normalize()
        else "Khách hàng mới"
    )


def replace_customer_rows(sheet, frame: pd.DataFrame) -> int:
    styles, formats = _reset_table(sheet, CUSTOMER_HEADERS)
    records = frame.reset_index(drop=True).to_dict(orient="records")
    for offset, record in enumerate(records):
        row = START_ROW + offset
        for col in range(1, max(CUSTOMER_HEADERS) + 1):
            sheet.cell(row, col)._style = copy(styles[col - 1])
            sheet.cell(row, col).number_format = formats[col - 1]

        values = {
            1: offset + 1,
            2: record.get("ten_nhan_vien", ""),
            3: record.get("ma_kh", ""),
            4: record.get("ten_kh", ""),
            5: _customer_type(record),
            6: int(record.get("order_count_2m", 0) or 0),
            7: float(record.get("max_order_2m_ktb", 0) or 0),
            8: float(record.get("total_order_2m_ktb", 0) or 0),
            9: f'=IF(OR(G{row}>=\'{PARAM_SHEET}\'!$B$2,H{row}>=\'{PARAM_SHEET}\'!$B$3),"ĐẠT","CHƯA ĐẠT")',
            10: "ĐÃ GHI TỒN" if bool(record.get("ghi_ton_2m", False)) else "CHƯA GHI TỒN",
            11: 0,
            12: 0,
            13: 0,
            14: bool(record.get("valid_sign_note_2m", False)),
            15: f'=IF(AND(OR(K{row}>=1,N{row}=TRUE),L{row}>=1),"ĐẠT","KHÔNG ĐẠT")',
            16: (
                f'=IF(F{row}=0,"Không Đạt",IF(E{row}="Khách hàng cũ",'
                f'IF(G{row}>=\'{PARAM_SHEET}\'!$B$2,"KHTC",IF(H{row}>=\'{PARAM_SHEET}\'!$B$3,"KHĐĐK","Không Đạt")),'
                f'IF(O{row}="KHÔNG ĐẠT","Không Đạt",IF(G{row}>=\'{PARAM_SHEET}\'!$B$2,"KHTC",IF(H{row}>=\'{PARAM_SHEET}\'!$B$3,"KHĐĐK","Không Đạt")))))'
            ),
            17: (
                f'=IF(P{row}="KHTC",IF(E{row}="Khách hàng cũ","Đạt KHTC (KH cũ có đơn ≥ "&\'{PARAM_SHEET}\'!$B$2&" KTB)",'
                f'"Đạt KHTC (KH mới có đơn ≥ "&\'{PARAM_SHEET}\'!$B$2&" KTB & đạt ảnh)"),'
                f'IF(P{row}="KHĐĐK",IF(E{row}="Khách hàng cũ","Đạt KHĐĐK (KH cũ cộng dồn ≥ "&\'{PARAM_SHEET}\'!$B$3&" KTB)",'
                f'"Đạt KHĐĐK (KH mới cộng dồn ≥ "&\'{PARAM_SHEET}\'!$B$3&" KTB & đạt ảnh)"),'
                f'IF(F{row}=0,IF(E{row}="Khách hàng mới","KH mới chưa có đơn hàng","KH cũ chưa có đơn hàng"),'
                f'IF(I{row}="CHƯA ĐẠT","Doanh số không đủ ngưỡng",'
                f'IF(AND(NOT(OR(K{row}>=1,N{row}=TRUE)),L{row}=0),IF(M{row}>=1,"Ảnh biển hiệu không đạt; Ảnh trưng bày không đạt","Không có ảnh biển hiệu; Không có ảnh trưng bày"),'
                f'IF(NOT(OR(K{row}>=1,N{row}=TRUE)),IF(M{row}>=1,"Ảnh biển hiệu không đạt","Không có ảnh biển hiệu"),'
                f'IF(L{row}=0,IF(M{row}>=1,"Ảnh trưng bày không đạt","Không có ảnh trưng bày"),"Doanh số không đủ ngưỡng")))))))'
            ),
            18: "Dò ảnh",
        }
        for col, value in values.items():
            sheet.cell(row, col, value)
        sheet.cell(row, 7).number_format = "0.00"
        sheet.cell(row, 8).number_format = "0.00"

    end_row = START_ROW + len(records) - 1 if records else START_ROW - 1
    sheet.freeze_panes = "A5"
    sheet.auto_filter.ref = f"A4:R{max(4, end_row)}"
    return end_row


def update_customer_image_formulas(
    sheet,
    detail_count: int,
    detail_frame: pd.DataFrame | None = None,
) -> None:
    detail_end = max(START_ROW, START_ROW + detail_count - 1)
    first_rows: dict[str, int] = {}
    if detail_frame is not None and not detail_frame.empty and "ma_kh" in detail_frame.columns:
        for offset, value in enumerate(detail_frame["ma_kh"].tolist(), start=START_ROW):
            key = str(value or "").strip()
            if key and key not in first_rows:
                first_rows[key] = offset

    for row in range(START_ROW, sheet.max_row + 1):
        customer = f"C{row}"
        base = (
            f"'{DETAIL_SHEET}'!$D$5:$D${detail_end},{customer},"
            f"'{DETAIL_SHEET}'!$I$5:$I${detail_end}"
        )
        sheet[f"K{row}"] = f'=COUNTIFS({base},"Bien_hieu")'
        sheet[f"L{row}"] = f'=COUNTIFS({base},"Trung_bay")'
        sheet[f"M{row}"] = f'=COUNTIFS({base},"Khong_dat")'
        cell = sheet[f"R{row}"]
        customer_code = str(sheet[f"C{row}"].value or "").strip()
        target_row = first_rows.get(customer_code)
        if target_row:
            cell.value = "Dò ảnh"
            cell.hyperlink = f"#'{DETAIL_SHEET}'!A{target_row}"
            cell.style = "Hyperlink"
        else:
            cell.value = "Không có ảnh"
            cell.hyperlink = None


def replace_summary_rows(sheet, customer_frame: pd.DataFrame, customer_end_row: int) -> None:
    styles, formats = _reset_table(sheet, SUMMARY_HEADERS)
    employees: list[str] = []
    if not customer_frame.empty and "ten_nhan_vien" in customer_frame.columns:
        employees = [
            str(value)
            for value in customer_frame["ten_nhan_vien"].dropna().drop_duplicates().tolist()
            if str(value).strip()
        ]
    customer_end = max(START_ROW, customer_end_row)
    for offset, employee in enumerate(employees):
        row = START_ROW + offset
        for col in range(1, max(SUMMARY_HEADERS) + 1):
            sheet.cell(row, col)._style = copy(styles[col - 1])
            sheet.cell(row, col).number_format = formats[col - 1]
        customer_range = f"'{CUSTOMER_SHEET}'!$B$5:$B${customer_end}"
        result_range = f"'{CUSTOMER_SHEET}'!$P$5:$P${customer_end}"
        reason_range = f"'{CUSTOMER_SHEET}'!$Q$5:$Q${customer_end}"
        type_range = f"'{CUSTOMER_SHEET}'!$E$5:$E${customer_end}"
        sheet.cell(row, 1, offset + 1)
        sheet.cell(row, 2, employee)
        sheet.cell(row, 3, f'=COUNTIF({customer_range},B{row})')
        sheet.cell(row, 4, f'=COUNTIFS({customer_range},B{row},{result_range},"<>Không Đạt")')
        sheet.cell(row, 5, f'=COUNTIFS({customer_range},B{row},{reason_range},"*chưa có đơn hàng*")')
        sheet.cell(row, 6, f'=COUNTIFS({customer_range},B{row},{reason_range},"Doanh số không đủ ngưỡng")')
        sheet.cell(row, 7, f'=COUNTIFS({customer_range},B{row},{reason_range},"Không có ảnh*")')
        sheet.cell(row, 8, f'=COUNTIFS({customer_range},B{row},{reason_range},"Ảnh*không đạt*")')
        sheet.cell(row, 9, f'=IF(\'{PARAM_SHEET}\'!$B$8=0,0,D{row}/\'{PARAM_SHEET}\'!$B$8)')
        sheet.cell(row, 10, f'=\'{PARAM_SHEET}\'!$B$9')
        sheet.cell(row, 11, f'=ROUND(MIN(I{row},1)*J{row},2)')
        sheet.cell(row, 12, f'=COUNTIFS({customer_range},B{row},{type_range},"Khách hàng mới",{result_range},"<>Không Đạt")')
        sheet.cell(row, 13, f'=MIN(L{row},\'{PARAM_SHEET}\'!$B$6)*\'{PARAM_SHEET}\'!$B$4')
        sheet.cell(row, 14, f'=COUNTIFS({customer_range},B{row},{type_range},"Khách hàng cũ",{result_range},"<>Không Đạt")')
        sheet.cell(row, 15, f'=MIN(N{row},\'{PARAM_SHEET}\'!$B$6)*\'{PARAM_SHEET}\'!$B$5')
        sheet.cell(row, 16, f'=MIN(M{row}+O{row},\'{PARAM_SHEET}\'!$B$7)')
        sheet.cell(
            row,
            17,
            f'=IF(I{row}>=1,"Đạt xuất sắc (100% công)",IF(I{row}>=0.8,"Hoàn thành tốt",IF(I{row}>=0.5,"Hoàn thành trung bình","Chưa đạt chỉ tiêu")))',
        )
        sheet.cell(row, 9).number_format = "0.00%"
        sheet.cell(row, 11).number_format = "0.00"
        for col in (13, 15, 16):
            sheet.cell(row, col).number_format = '#,##0 "VNĐ"'
    end_row = START_ROW + len(employees) - 1 if employees else START_ROW - 1
    sheet.freeze_panes = "A5"
    sheet.auto_filter.ref = f"A4:Q{max(4, end_row)}"
