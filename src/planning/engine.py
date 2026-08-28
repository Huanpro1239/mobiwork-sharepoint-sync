from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sharepoint import SharePointClient

from .config import PlanningConfig
from .excel_io import read_sheet_rows, write_shadow_workbook
from .normalize import normalize_code, to_number
from .vba_port import (
    aggregate_gui_kho,
    aggregate_xuat_kho,
    build_divisor_map,
    extract_open_po,
    load_source_stock_first,
    map_gui_kho_to_products,
    map_ton_tt,
    nokho_balance,
    nokho_col_d,
    nokho_col_e,
    sum_two_divided_stocks,
)

LOG = logging.getLogger("planning_engine")


def _sheet_rows(data: bytes, sheet: str, start: int, max_col: int) -> list[dict[str, Any]]:
    return read_sheet_rows(data, sheet, min_row=start, max_col=max_col, data_only=True)


def run_shadow(config: PlanningConfig, client: SharePointClient, drive_id: str, output_dir: Path) -> dict[str, Any]:
    """Run the low-risk V1 shadow pipeline without replacing the production workbook."""
    output_dir.mkdir(parents=True, exist_ok=True)

    def download(path: str) -> bytes:
        content = client.download_file_bytes(drive_id, path)
        if content is None:
            raise FileNotFoundError(f"SharePoint file not found: {path}")
        return content

    master = download(config.planning_master_path)
    dmsp = _sheet_rows(master, "DMSP", 2, 14)
    divisors = build_divisor_map(dmsp)
    product_codes = [normalize_code(row.get("C")) for row in dmsp if normalize_code(row.get("C"))]

    cache: dict[str, bytes] = {}
    for key, source in config.sources.items():
        cache[key] = download(source.path)
        LOG.info("Downloaded planning source %s", key)

    gui_dau = aggregate_gui_kho(_sheet_rows(cache["gui_kho_dau_thang"], "Chi tiet", 2, 33))
    gui_hien = aggregate_gui_kho(_sheet_rows(cache["gui_kho_hien_tai"], "Chi tiet", 2, 33))
    gui_dau_mapped = map_gui_kho_to_products(product_codes, gui_dau)
    gui_hien_mapped = map_gui_kho_to_products(product_codes, gui_hien)

    no_d = nokho_col_d(_sheet_rows(cache["hang_nhap_truoc"], "SUM", 5, 21), product_codes)
    no_e = nokho_col_e(_sheet_rows(cache["ton_thuc_te_hien_tai"], _first_sheet_name(cache["ton_thuc_te_hien_tai"]), 5, 15), product_codes)
    ton_vikoda_rows = _sheet_rows(cache["ton_vikoda"], "Sheet1", 11, 13)
    no_f_raw = load_source_stock_first(ton_vikoda_rows, "I")
    no_f = {code: (to_number(no_f_raw.get(code)) / to_number(divisors.get(code)) if to_number(divisors.get(code)) else 0.0) for code in product_codes}
    no_g = nokho_balance(no_d, no_e, no_f)

    fc_m = sum_two_divided_stocks(
        product_codes,
        _sheet_rows(cache["xnt_vikoda"], "Sheet1", 11, 13),
        _sheet_rows(cache["xnt_vkd"], "Sheet1", 11, 13),
        divisors,
        value_column="M",
    )

    tinh_d = _divided_map(product_codes, _sheet_rows(cache["ton_vikoda"], "Sheet1", 11, 13), divisors, "M", "none")
    tinh_e = _divided_map(product_codes, _sheet_rows(cache["ton_vkd"], "Sheet1", 11, 13), divisors, "M", "2to1")
    tinh_f = sum_two_divided_stocks(
        product_codes,
        _sheet_rows(cache["ton_ban_duoc_vikoda"], "Sheet1", 11, 13),
        _sheet_rows(cache["ton_ban_duoc_vkd"], "Sheet1", 11, 13),
        divisors,
        value_column="K",
    )

    po_rows = extract_open_po(_sheet_rows(cache["po_mua_hang"], "REPORT_DONMUAHANG", 6, 28))
    xuat = aggregate_xuat_kho(
        _sheet_rows(cache["xuat_kho"], "REPORT_XUATBANHANG", 6, 21),
        _sheet_rows(cache["xuat_kho_vikoda"], "REPORT_XUATBANHANG", 6, 21),
    )
    ton_tt = map_ton_tt(
        _sheet_rows(cache["ton_tt_dau_thang"], _first_sheet_name(cache["ton_tt_dau_thang"]), 5, 15),
        product_codes,
    )

    stock_rows = [
        {
            "Ma SP": code,
            "Gui kho dau thang": gui_dau_mapped.get(code, 0.0),
            "Gui kho hien tai": gui_hien_mapped.get(code, 0.0),
            "No kho D": no_d.get(code, 0.0),
            "No kho E": no_e.get(code, 0.0),
            "No kho F": no_f.get(code, 0.0),
            "No kho hien tai G": no_g.get(code, 0.0),
            "FC ton dau thang M": fc_m.get(code, 0.0),
            "Tinh ung hang D": tinh_d.get(code, 0.0),
            "Tinh ung hang E": tinh_e.get(code, 0.0),
            "Tinh ung hang F": tinh_f.get(code, 0.0),
            "Ton TT KHSX I": ton_tt.get(code, 0.0),
        }
        for code in product_codes
    ]

    output_file = output_dir / "planning_shadow.xlsx"
    write_shadow_workbook({"Stock_Reconciliation": stock_rows, "PO_Open": po_rows, "XuatKho_ThangTruoc": xuat}, output_file)

    manifest = {
        "status": "shadow_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "planning_master_path": config.planning_master_path,
        "product_count": len(product_codes),
        "open_po_rows": len(po_rows),
        "shipment_product_rows": len(xuat),
        "output_file": output_file.name,
        "scope": "VBA ETL + stock reconciliation; production scheduling formulas not yet cut over",
    }
    (output_dir / "planning_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _first_sheet_name(workbook_bytes: bytes) -> str:
    from io import BytesIO
    import openpyxl
    wb = openpyxl.load_workbook(BytesIO(workbook_bytes), read_only=True, data_only=True, keep_vba=True)
    return wb.sheetnames[0]


def _divided_map(product_codes: list[str], rows: list[dict[str, Any]], divisors: dict[str, float], value_col: str, mode: str) -> dict[str, float]:
    raw = load_source_stock_first(rows, value_col, code_mode=mode)
    out: dict[str, float] = {}
    for code in product_codes:
        divisor = to_number(divisors.get(code))
        out[code] = to_number(raw.get(code)) / divisor if divisor else 0.0
    return out
