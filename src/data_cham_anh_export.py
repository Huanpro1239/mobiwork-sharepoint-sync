from __future__ import annotations

import os
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

if __package__:
    from .excel_export import _format_sheet, _validate_excel_size
    from .image_sync import _iter_urls
    from .mobiwork import ReportConfig
    from .monthly_master import master_filename
else:
    from excel_export import _format_sheet, _validate_excel_size
    from image_sync import _iter_urls
    from mobiwork import ReportConfig
    from monthly_master import master_filename


DATA_ANH_COLUMNS = [
    "ten_nhan_vien",
    "ngay",
    "ma_kh",
    "ten_kh",
    "vung",
    "stt_hinh",
    "hinh_anh",
    "so_hinh",
    "ghi_ton",
    "ghi_chu",
]

DATA_DON_HANG_COLUMNS = [
    "ma_kh",
    "ten_kh",
    "ngay_dat",
    "ten_nhom",
    "ma_nv_dat",
    "ten_nguoi_dat",
    "ma_nv_duyet",
    "nguoi_duyet",
    "ngay_duyet",
    "ngay_tao",
    "ma_nguoi_tao",
    "nguoi_tao",
    "dien_giai",
    "ma_sp",
    "ten_sp",
    "ma_dvt",
    "ma_kho_xuat",
    "so_luong",
    "don_gia",
]

DEFAULT_ROOT_FOLDER = "05_DataChamAnh"
_IMAGE_CODE_COLUMNS = {"ma_kh"}
_BILL_CODE_COLUMNS = {
    "ma_kh",
    "ma_nv_dat",
    "ma_nv_duyet",
    "ma_nguoi_tao",
    "ma_sp",
    "ma_kho_xuat",
}
_BILL_NUMERIC_COLUMNS = {"so_luong", "don_gia"}


def _positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not number.is_integer() or number <= 0:
        return None
    return int(number)


def _first_present(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = record.get(name)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        return value
    return None


def build_data_anh_frame(source: pd.DataFrame) -> pd.DataFrame:
    """Create the flat image sheet used by the approved business workbook.

    Sales region is sourced only from ``vung``. ``loai_kh`` is intentionally ignored
    because it is a customer classification, not a sales region. One source row may
    contain multiple image URLs; each URL becomes one workbook row.
    """
    if source.empty:
        return pd.DataFrame(columns=DATA_ANH_COLUMNS)
    if "vung" not in source.columns:
        raise ValueError(
            "Visit monthly master is missing 'vung'. Refusing to use loai_kh as a region."
        )

    rows: list[dict[str, Any]] = []
    for record in source.to_dict("records"):
        urls = list(_iter_urls(record.get("hinh_anh")))
        if not urls:
            continue

        source_sequence = _positive_int(record.get("stt_hinh"))
        source_total = _positive_int(record.get("so_hinh"))
        total_images = max(source_total or 0, len(urls)) or len(urls)

        for image_index, image_url in enumerate(urls, start=1):
            image_sequence = (
                source_sequence
                if source_sequence is not None and len(urls) == 1
                else image_index
            )
            rows.append(
                {
                    "ten_nhan_vien": record.get("ten_nhan_vien"),
                    "ngay": _first_present(record, "ngay", "_sync_date"),
                    "ma_kh": record.get("ma_kh"),
                    "ten_kh": record.get("ten_kh"),
                    "vung": record.get("vung"),
                    "stt_hinh": image_sequence,
                    "hinh_anh": image_url,
                    "so_hinh": total_images,
                    "ghi_ton": record.get("ghi_ton"),
                    "ghi_chu": record.get("ghi_chu"),
                }
            )

    frame = pd.DataFrame(rows, columns=DATA_ANH_COLUMNS)
    for column in _IMAGE_CODE_COLUMNS.intersection(frame.columns):
        frame[column] = frame[column].astype("string")
    if "stt_hinh" in frame.columns:
        frame["stt_hinh"] = pd.to_numeric(frame["stt_hinh"], errors="coerce").astype("Int64")
    if "so_hinh" in frame.columns:
        frame["so_hinh"] = pd.to_numeric(frame["so_hinh"], errors="coerce").astype("Int64")
    _validate_excel_size(frame, "Data_anh")
    return frame


def build_data_don_hang_frame(source: pd.DataFrame) -> pd.DataFrame:
    """Project the published Bill detail master to the exact approved sample schema."""
    frame = source.reindex(columns=DATA_DON_HANG_COLUMNS).copy()
    for column in _BILL_CODE_COLUMNS.intersection(frame.columns):
        frame[column] = frame[column].astype("string")
    for column in _BILL_NUMERIC_COLUMNS.intersection(frame.columns):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    _validate_excel_size(frame, "Data_don_hang")
    return frame


def _set_data_anh_date_format(writer: pd.ExcelWriter) -> None:
    worksheet = writer.book["Data_anh"]
    header_map = {
        worksheet.cell(row=1, column=column).value: column
        for column in range(1, worksheet.max_column + 1)
    }
    date_column = header_map.get("ngay")
    if not date_column:
        return
    for row in range(2, worksheet.max_row + 1):
        worksheet.cell(row=row, column=date_column).number_format = "dd/mm/yyyy"


def write_data_cham_anh_workbook(
    data_anh: pd.DataFrame,
    data_don_hang: pd.DataFrame,
    target_date: date,
    *,
    output_dir: Path = Path("output"),
) -> Path:
    """Write one monthly workbook with the exact two approved sheet names."""
    image_frame = data_anh.reindex(columns=DATA_ANH_COLUMNS).copy()
    bill_frame = data_don_hang.reindex(columns=DATA_DON_HANG_COLUMNS).copy()
    _validate_excel_size(image_frame, "Data_anh")
    _validate_excel_size(bill_frame, "Data_don_hang")

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"Data_cham_anh_{target_date:%Y-%m}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        image_frame.to_excel(writer, sheet_name="Data_anh", index=False)
        _format_sheet(writer, "Data_anh")
        _set_data_anh_date_format(writer)

        bill_frame.to_excel(writer, sheet_name="Data_don_hang", index=False)
        _format_sheet(writer, "Data_don_hang")
    return path


def _report_by_key(reports: list[ReportConfig], key: str) -> ReportConfig:
    report = next((item for item in reports if item.key == key and item.enabled), None)
    if report is None:
        raise ValueError(f"Required report {key!r} is not enabled")
    return report


def _monthly_master_path(cfg: ReportConfig, target_date: date) -> str:
    folder = cfg.folder.strip("/")
    filename = master_filename(cfg.name, target_date)
    return f"{folder}/{target_date:%Y}/{target_date:%m}/{filename}"


def _read_sheet(content: bytes, sheet_name: str, label: str) -> pd.DataFrame:
    if not content:
        raise ValueError(f"{label} workbook is empty")
    try:
        return pd.read_excel(BytesIO(content), sheet_name=sheet_name, engine="openpyxl")
    except ValueError as exc:
        raise ValueError(f"{label} workbook is missing sheet {sheet_name!r}") from exc


def publish_data_cham_anh_month(
    reports: list[ReportConfig],
    sharepoint: Any,
    drive_id: str,
    target_date: date,
    *,
    root_folder: str | None = None,
    output_dir: Path = Path("output"),
) -> dict[str, Any]:
    """Build the combined workbook from already-published Visit and Bill masters."""
    visit = _report_by_key(reports, "visit")
    bill = _report_by_key(reports, "bill")
    visit_path = _monthly_master_path(visit, target_date)
    bill_path = _monthly_master_path(bill, target_date)

    visit_content = sharepoint.download_file_bytes(drive_id, visit_path)
    if visit_content is None:
        raise FileNotFoundError(f"SharePoint Visit monthly master not found: {visit_path}")
    bill_content = sharepoint.download_file_bytes(drive_id, bill_path)
    if bill_content is None:
        raise FileNotFoundError(f"SharePoint Bill monthly master not found: {bill_path}")

    visit_frame = _read_sheet(visit_content, "Data", "Visit")
    bill_detail = _read_sheet(bill_content, "ChiTietSP", "Bill")
    data_anh = build_data_anh_frame(visit_frame)
    data_don_hang = build_data_don_hang_frame(bill_detail)
    path = write_data_cham_anh_workbook(
        data_anh,
        data_don_hang,
        target_date,
        output_dir=output_dir,
    )

    configured_root = (
        root_folder
        or os.environ.get("DATA_CHAM_ANH_ROOT_FOLDER", "")
        or DEFAULT_ROOT_FOLDER
    ).strip("/")
    if not configured_root:
        raise ValueError("DATA_CHAM_ANH_ROOT_FOLDER must not be empty")
    remote_folder = f"{configured_root}/{target_date:%Y}/{target_date:%m}"
    uploaded = sharepoint.upload_file(drive_id, path, remote_folder)
    return {
        "status": "success",
        "month": target_date.strftime("%Y-%m"),
        "filename": path.name,
        "local_size_bytes": path.stat().st_size,
        "data_anh_rows": len(data_anh),
        "data_don_hang_rows": len(data_don_hang),
        "visit_source_path": visit_path,
        "bill_source_path": bill_path,
        "remote_folder": remote_folder,
        "remote_size_bytes": uploaded.get("size"),
        "verification_mode": uploaded.get("verification_mode"),
        "semantic_match": uploaded.get("semantic_match"),
        "upload_skipped": bool(uploaded.get("upload_skipped", False)),
        "web_url": uploaded.get("webUrl"),
    }
