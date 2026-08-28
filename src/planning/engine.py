from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from src.sharepoint import SharePointClient

from .config import PlanningConfig
from .excel_io import read_sheet_rows, write_shadow_workbook
from .formula_port import (
    aggregate_open_po,
    build_abc_rows,
    build_algorithmic_daily_schedule,
    build_daily_material_allocation,
    build_fc_end_stock,
    build_finished_goods_projection,
    build_material_inbound_plan,
    build_purchase_plan,
    build_weekly_production_plan,
    forecast_by_month,
    material_demand_periods,
    material_direct_projection,
    standardize_direct_bom,
    standardize_flat_bom,
)
from .normalize import clean_text, normalize_code, to_number
from .rgb_scheduler import build_rgb_daily_schedule
from .source_refresh import (
    find_column_by_header,
    first_sheet_name,
    material_stock_last,
    sales_actual_cases,
    sheet_name_by_index,
)
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
VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _sheet_rows(
    data: bytes, sheet: str, start: int, max_col: int
) -> list[dict[str, Any]]:
    return read_sheet_rows(
        data, sheet, min_row=start, max_col=max_col, data_only=True
    )


def _plan_month(master: bytes, fallback: int) -> int:
    rows = _sheet_rows(master, "Ke hoach SX tuan", 2, 2)
    if not rows:
        return fallback
    text = clean_text(rows[0].get("B"))
    match = re.search(r"(\d{1,2})", text)
    if not match:
        return fallback
    value = int(match.group(1))
    return value if 1 <= value <= 12 else fallback


def _date_only(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _leadtime_map(dmsp_rows: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in dmsp_rows:
        code = normalize_code(row.get("C"))
        if code and code not in result:
            result[code] = to_number(row.get("J"))
    return result


def _sales_total_map(sales_rows: list[dict[str, Any]]) -> dict[str, float]:
    output: dict[str, float] = {}
    for row in sales_rows:
        code = normalize_code(row.get("Ma SP"))
        if not code:
            continue
        output[code] = sum(
            to_number(value)
            for key, value in row.items()
            if key != "Ma SP"
        )
    return output


def run_shadow(
    config: PlanningConfig,
    client: SharePointClient,
    drive_id: str,
    output_dir: Path,
) -> dict[str, Any]:
    """Run the V2 planning shadow pipeline from live SharePoint inputs.

    V2 keeps the production workbook read-only. It ports the nine source-refresh
    steps plus the formula chain for Tinh ung hang, material planning, ABC,
    purchasing and the formula-driven part of production scheduling.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    def download(path: str) -> bytes:
        content = client.download_file_bytes(drive_id, path)
        if content is None:
            raise FileNotFoundError(f"SharePoint file not found: {path}")
        return content

    master = download(config.planning_master_path)
    dmsp = _sheet_rows(master, "DMSP", 2, 14)
    divisors = build_divisor_map(dmsp)
    product_codes = [
        normalize_code(row.get("C"))
        for row in dmsp
        if normalize_code(row.get("C"))
    ]

    cache: dict[str, bytes] = {}
    for key, source in config.sources.items():
        cache[key] = download(source.path)
        LOG.info("Downloaded planning source %s", key)

    # Call_All step 1: BCBANHANG -> FC thang nay T:Y.
    sales1 = _sheet_rows(cache["ban_hang"], "Sheet1", 10, 50)
    sales2 = _sheet_rows(cache["ban_hang_vikoda"], "Sheet1", 10, 50)
    invoice_col = find_column_by_header(
        cache["ban_hang_vikoda"],
        "Sheet1",
        "LoaiHoaDon",
        first_row=1,
        last_row=9,
    )
    for row in sales2:
        row["LoaiHoaDon"] = row.get(invoice_col)
    fc_rows_current = _sheet_rows(master, "FC thang nay", 3, 25)
    if not fc_rows_current:
        raise RuntimeError("FC thang nay has no row 3 headers/data")
    channel_headers = [
        fc_rows_current[0].get(col)
        for col in ("T", "U", "V", "W", "X", "Y")
    ]
    sales_cases = sales_actual_cases(
        sales1, sales2, product_codes, channel_headers, divisors
    )

    # Call_All step 5: Tinh_NVL -> Ke hoach nhap NVL D.
    material_sheet = sheet_name_by_index(cache["ton_nvl"], 2)
    material_source_rows = _sheet_rows(
        cache["ton_nvl"], material_sheet, 8, 8
    )
    material_dest_rows = _sheet_rows(master, "Ke hoach nhap NVL", 2, 1)
    material_codes = [
        row.get("A")
        for row in material_dest_rows
        if row.get("A") not in (None, "")
    ]
    material_stock = material_stock_last(
        material_source_rows, material_codes
    )
    material_stock_rows = [
        {
            "Ma NVL": normalize_code(code),
            "Ton NVL D": material_stock.get(normalize_code(code)),
        }
        for code in material_codes
    ]

    # Call_All step 2: Gui kho -> FC thang nay O/P.
    gui_dau = aggregate_gui_kho(
        _sheet_rows(cache["gui_kho_dau_thang"], "Chi tiet", 2, 33)
    )
    gui_hien = aggregate_gui_kho(
        _sheet_rows(cache["gui_kho_hien_tai"], "Chi tiet", 2, 33)
    )
    gui_dau_mapped = map_gui_kho_to_products(product_codes, gui_dau)
    gui_hien_mapped = map_gui_kho_to_products(product_codes, gui_hien)

    # Call_All step 3: No kho D/E/F/G.
    no_d = nokho_col_d(
        _sheet_rows(cache["hang_nhap_truoc"], "SUM", 5, 21),
        product_codes,
    )
    no_e = nokho_col_e(
        _sheet_rows(
            cache["ton_thuc_te_hien_tai"],
            first_sheet_name(cache["ton_thuc_te_hien_tai"]),
            5,
            15,
        ),
        product_codes,
    )
    ton_vikoda_rows = _sheet_rows(
        cache["ton_vikoda"], "Sheet1", 11, 13
    )
    no_f_raw = load_source_stock_first(ton_vikoda_rows, "I")
    no_f = {
        code: (
            to_number(no_f_raw.get(code)) / to_number(divisors.get(code))
            if to_number(divisors.get(code))
            else 0.0
        )
        for code in product_codes
    }
    no_g = nokho_balance(no_d, no_e, no_f)

    # Call_All step 7: FC thang nay M (ton dau thang).
    fc_m = sum_two_divided_stocks(
        product_codes,
        _sheet_rows(cache["xnt_vikoda"], "Sheet1", 11, 13),
        _sheet_rows(cache["xnt_vkd"], "Sheet1", 11, 13),
        divisors,
        value_column="M",
    )

    # Call_All step 6: Tinh ung hang D/E/F.
    tinh_d = _divided_map(
        product_codes,
        _sheet_rows(cache["ton_vikoda"], "Sheet1", 11, 13),
        divisors,
        "M",
        "none",
    )
    tinh_e = _divided_map(
        product_codes,
        _sheet_rows(cache["ton_vkd"], "Sheet1", 11, 13),
        divisors,
        "M",
        "2to1",
    )
    tinh_f = sum_two_divided_stocks(
        product_codes,
        _sheet_rows(
            cache["ton_ban_duoc_vikoda"], "Sheet1", 11, 13
        ),
        _sheet_rows(cache["ton_ban_duoc_vkd"], "Sheet1", 11, 13),
        divisors,
        value_column="K",
    )

    # Call_All step 4: open PO.
    po_rows = extract_open_po(
        _sheet_rows(
            cache["po_mua_hang"], "REPORT_DONMUAHANG", 6, 28
        )
    )

    # Call_All step 8: prior-month ERP outbound.
    xuat = aggregate_xuat_kho(
        _sheet_rows(
            cache["xuat_kho"], "REPORT_XUATBANHANG", 6, 21
        ),
        _sheet_rows(
            cache["xuat_kho_vikoda"], "REPORT_XUATBANHANG", 6, 21
        ),
    )

    # Call_All step 9: beginning stock -> weekly production plan I.
    ton_tt = map_ton_tt(
        _sheet_rows(
            cache["ton_tt_dau_thang"],
            first_sheet_name(cache["ton_tt_dau_thang"]),
            5,
            15,
        ),
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

    # V2 formula engine starts here.
    now_vietnam = datetime.now(VIETNAM_TZ)
    today = now_vietnam.date()
    current_month = today.month
    fc_master = _sheet_rows(master, "FC", 2, 18)
    forecast_current = forecast_by_month(fc_master, current_month)
    forecast_m1 = forecast_by_month(
        fc_master, (current_month % 12) + 1
    )
    forecast_m2 = forecast_by_month(
        fc_master, ((current_month + 1) % 12) + 1
    )
    forecast_m3 = forecast_by_month(
        fc_master, ((current_month + 2) % 12) + 1
    )
    actual_sales = _sales_total_map(sales_cases)

    projection_rows = build_finished_goods_projection(
        product_codes,
        stock_vikoda=tinh_d,
        stock_vkd=tinh_e,
        plant_stock=tinh_f,
        actual_sales=actual_sales,
        forecast_current=forecast_current,
        forecast_m1=forecast_m1,
        forecast_m2=forecast_m2,
        forecast_m3=forecast_m3,
        warehouse_debt=no_g,
    )

    dm_nvl = _sheet_rows(master, "DM NVL", 2, 6)
    flat_bom = standardize_flat_bom(
        _sheet_rows(master, "Flat BOM", 2, 4)
    )
    direct_bom = standardize_direct_bom(
        _sheet_rows(master, "BOM", 2, 5)
    )
    demand_periods = material_demand_periods(
        projection_rows, flat_bom
    )
    direct_run_need, material_debt = material_direct_projection(
        projection_rows, direct_bom
    )
    open_po, po_lines = aggregate_open_po(po_rows)

    material_inbound_rows = build_material_inbound_plan(
        dm_nvl,
        stock=material_stock,
        direct_run_need=direct_run_need,
        open_po=open_po,
        material_debt=material_debt,
    )
    abc_rows = build_abc_rows(
        dm_nvl,
        demand_periods=demand_periods,
        stock=material_stock,
        open_po=open_po,
        debt=material_debt,
    )

    holidays: set[date] = set()
    for row in _sheet_rows(master, "Ngay le", 6, 1):
        value = _date_only(row.get("A"))
        if value is not None:
            holidays.add(value)

    purchase_rows = build_purchase_plan(
        dm_nvl,
        demand_periods=demand_periods,
        stock=material_stock,
        open_po=open_po,
        debt=material_debt,
        abc_rows=abc_rows,
        today=today,
        holidays=holidays,
    )

    leadtime = _leadtime_map(dmsp)
    daily_sales, projected_end_stock = build_fc_end_stock(
        product_codes,
        current_forecast=forecast_current,
        gui_kho_begin=gui_dau_mapped,
        leadtime=leadtime,
    )

    plan_month = _plan_month(master, current_month)
    plan_year = today.year
    weekly_forecast = forecast_by_month(fc_master, plan_month)
    opening_book_stock = dict(fc_m)

    # Workbook special-case: code 130100149 represents the grouped RGB 480ml
    # family in KHSX. FC column R=1 marks the component SKUs.
    special_group = {
        normalize_code(row.get("B"))
        for row in fc_master
        if to_number(row.get("R")) == 1
    }
    if special_group:
        opening_book_stock["130100149"] = sum(
            to_number(fc_m.get(code)) for code in special_group
        )
        weekly_forecast["130100149"] = sum(
            to_number(weekly_forecast.get(code))
            for code in special_group
        )

    weekly_config = _sheet_rows(
        master, "Ke hoach SX tuan", 4, 10
    )
    weekly_rows = build_weekly_production_plan(
        weekly_config,
        plan_month=plan_month,
        plan_year=plan_year,
        actual_stock=ton_tt,
        opening_book_stock=opening_book_stock,
        forecast=weekly_forecast,
        projected_end_stock=projected_end_stock,
        warehouse_debt=no_g,
        daily_sales=daily_sales,
        leadtime=leadtime,
    )
    daily_auto_rows = build_algorithmic_daily_schedule(
        weekly_rows,
        plan_year=plan_year,
        plan_month=plan_month,
    )
    rgb_daily_rows = build_rgb_daily_schedule(
        weekly_rows,
        plan_year=plan_year,
        plan_month=plan_month,
    )
    daily_auto_rows.extend(rgb_daily_rows)
    unsupported_weekly_rows = [
        row
        for row in weekly_rows
        if to_number(row.get("SL SX tron me/ca")) > 0
        and clean_text(row.get("Chuyen"))
        not in {"KHS", "PET 9000", "Galon", "RGB"}
    ]

    allocation_start = max(
        today, date(plan_year, plan_month, 1)
    )
    daily_material_rows = build_daily_material_allocation(
        dm_nvl,
        flat_bom_rows=flat_bom,
        daily_product_rows=daily_auto_rows,
        stock=material_stock,
        po_lines=po_lines,
        start_date=allocation_start,
        horizon_days=45,
    )

    output_file = output_dir / "planning_shadow.xlsx"
    write_shadow_workbook(
        {
            "Stock_Reconciliation": stock_rows,
            "Sales_Actual_Cases": sales_cases,
            "Material_Stock": material_stock_rows,
            "PO_Open": po_rows,
            "XuatKho_ThangTruoc": xuat,
            "TinhUngHang_V2": projection_rows,
            "KeHoachNhapNVL_V2": material_inbound_rows,
            "PhanTichABC_V2": abc_rows,
            "MuaHang_V2": purchase_rows,
            "KeHoachSXTuan_V2": weekly_rows,
            "KeHoachSXNgay_Auto": daily_auto_rows,
            "PhanBoNVLNgay_V2": daily_material_rows,
            "KHSX_ChuaAuto": unsupported_weekly_rows,
        },
        output_file,
    )

    manifest = {
        "status": "shadow_v2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "generated_at_vietnam": now_vietnam.isoformat(),
        "planning_master_path": config.planning_master_path,
        "product_count": len(product_codes),
        "sales_product_rows": len(sales_cases),
        "material_stock_rows": len(material_stock_rows),
        "open_po_rows": len(po_rows),
        "shipment_product_rows": len(xuat),
        "finished_projection_rows": len(projection_rows),
        "abc_rows": len(abc_rows),
        "purchase_rows": len(purchase_rows),
        "weekly_production_rows": len(weekly_rows),
        "auto_daily_rows": len(daily_auto_rows),
        "rgb_auto_rows": len(rgb_daily_rows),
        "daily_material_rows": len(daily_material_rows),
        "unsupported_weekly_rows": len(unsupported_weekly_rows),
        "plan_month": plan_month,
        "plan_year": plan_year,
        "output_file": output_file.name,
        "scope": (
            "V2 ports MRP/ABC/purchasing and formula-driven KHS/PET/Galon/RGB "
            "production scheduling. Existing manually curated daily schedule "
            "remains comparison-only and is not overwritten."
        ),
    }
    (output_dir / "planning_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def _divided_map(
    product_codes: list[str],
    rows: list[dict[str, Any]],
    divisors: dict[str, float],
    value_col: str,
    mode: str,
) -> dict[str, float]:
    raw = load_source_stock_first(
        rows, value_col, code_mode=mode
    )
    out: dict[str, float] = {}
    for code in product_codes:
        divisor = to_number(divisors.get(code))
        out[code] = (
            to_number(raw.get(code)) / divisor if divisor else 0.0
        )
    return out
