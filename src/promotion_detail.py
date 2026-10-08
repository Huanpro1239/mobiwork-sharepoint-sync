from __future__ import annotations

import ast
import json
import logging
import os
import re
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

from data_cham_anh_export import _monthly_master_path
from customer_catalogue import enrich_customer_config
from main import load_reports
from monthly_master import master_filename
from promotion_bonus import _api_total, _expect_object_list, _frame, fetch_programs, load_config as load_bonus_config
from promotion_months import discover_bill_months, select_order_month
from promotion_workbook import write_detail_workbook
from mobiwork import MobiWorkClient
from region_mapping import employee_prefix, load_region_map
from run_all_reports import incremental_target_dates
from run_data_cham_anh import month_anchors
import bosung_mapping as bosung
from sales_structure import enrich_employee_config
from sharepoint_semantic import SemanticSharePointClient

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/promotion_detail.json"
COLUMNS = ["Vùng", "Tỉnh", "SS Code", "SS Name", "DB Code", "Tên NPP", "Route",
           "Tên Nhân viên", "Mã NV", "Mã CTKM", "Mã Khách hàng", "Tên Khách hàng",
           "Địa chỉ", "Số ĐT", "Loại KH", "Ngày Đơn hàng", "Mã Đơn hàng", "Brand",
           "Package", "Mã sản phẩm", "Tên Sản phẩm", "Số lượng SELL-OUT", "THÀNH TIỀN",
           "Sản phẩm Tặng", "Tên Sản phẩm Tặng", "Số lượng Khuyến mãi"]
LOG = logging.getLogger("promotion_detail")
# A missing pack conversion leaves that line's quantity blank and is listed in CanBoSung,
# but no longer blocks publishing the whole month (other source errors still do).
UNIT_GAP = "Thiếu quy đổi sang KÉT/THÙNG/BÌNH"
SALES_FIELDS = ("Vùng", "SS Code", "SS Name", "DB Code", "Tên NPP")
NPP_UNIT = re.compile(r"^[A-Z]-[A-Z]+-\d+$")


def warehouse(row: dict[str, Any]) -> str:
    """The order's issuing warehouse: the distributor (NPP) unit at the time of sale."""
    for field in ("ma_kho_xuat", "ma_kho_xuat_km"):
        value = re.sub(r"^KM\s*-\s*", "", text(row.get(field))).strip()
        if NPP_UNIT.match(value):
            return value
    return ""


def sales_layers(row: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Vùng/SS/NPP sources, lowest priority first.

    The order's warehouse identifies its NPP even after staff leave or move, so it replaces
    the employee's *current* unit. Explicit config and the user's BoSung_Mapping file win.
    """
    employee_code = text(row.get("ma_nv_dat"))
    code = warehouse(row)
    if code:
        base = {"DB Code": code, **cfg.get("npp_units", {}).get(code, {}),
                **cfg.get("npp_overrides", {}).get(code, {})}
    else:
        base = cfg.get("employees", {}).get(employee_code, {})
    explicit = cfg.get("employees_explicit", cfg.get("employees", {})).get(employee_code, {})
    return [base, explicit, cfg.get("employee_overrides", {}).get(employee_code, {})]


def resolve_sales(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for layer in sales_layers(row, cfg):
        for field in SALES_FIELDS:
            if text(layer.get(field)):
                result[field] = text(layer[field])
    return result


def load_config() -> dict[str, Any]:
    path = Path(os.environ.get("PROMOTION_DETAIL_CONFIG", str(CONFIG)))
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or type(cfg.get("publish_enabled")) is not bool:
        raise ValueError("publish_enabled must be an explicit boolean")
    for flag in ("fetch_product_catalogue", "fetch_customer_catalogue", "fetch_program_catalogue",
                 "fetch_sales_structure", "allow_incomplete_publish", "bosung_mapping"):
        if type(cfg.get(flag, False)) is not bool:
            raise ValueError(f"{flag} must be an explicit boolean")
    for key in ("employees", "customers", "customer_codes", "products", "unit_conversions", "program_codes"):
        mapping = cfg.get(key, {})
        if not isinstance(mapping, dict):
            raise ValueError(f"{key} must be an object")
        for value in mapping.values():
            if key == "program_codes":
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("Program code mappings must be nonempty strings")
            elif not isinstance(value, dict):
                raise ValueError(f"{key} mapping entries must be objects")
    cfg["employee_regions"] = load_region_map(
        os.environ.get("EMPLOYEE_REGION_CONFIG") or str(ROOT / "config/employee_regions.json")
    )
    return cfg


def text(value: Any) -> str:
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return ""
    return str(value).strip()


def number(value: Any) -> Decimal:
    try:
        result = Decimal(text(value))
    except InvalidOperation as exc:
        raise ValueError("Missing or invalid numeric source value") from exc
    if not result.is_finite():
        raise ValueError("Non-finite numeric source value")
    return result


def promotion_list(value: Any) -> list[dict[str, Any]]:
    if not text(value):
        return []
    if isinstance(value, list):
        result = value
    else:
        try:
            result = json.loads(str(value))
        except ValueError:
            result = ast.literal_eval(str(value))
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise ValueError("promotion must contain an array of objects")
    return result


def program_code(value: str, cfg: dict[str, Any]) -> str:
    mapped = cfg.get("program_codes", {}).get(value)
    if mapped:
        return mapped
    # Recognize the business-code convention in the supplied template, retaining _Q3.
    match = re.match(r"^(\d+/TB/GT/\d+/\d{4}(?:_Q[1-4])?)(?:_|$)", value)
    return match.group(1) if match else value


def enrich_program_config(client: MobiWorkClient, cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve internal program IDs to business codes/names from the DMS catalogue."""
    result = json.loads(json.dumps(cfg))
    programs = fetch_programs(client, load_bonus_config())
    mapping = result.setdefault("program_codes", {})
    for program in programs:
        identity, name = text(program.get("_id")), text(program.get("name"))
        if identity and name:
            mapping.setdefault(identity, program_code(name, cfg))
    result["program_catalogue_count"] = len(programs)
    return result


def is_gift(row: dict[str, Any]) -> bool:
    flag = text(row.get("is_km")).casefold()
    if flag not in {"", "true", "false", "1", "0"}:
        raise ValueError("Unrecognized is_km source flag")
    label = text(row.get("loai_hang")).casefold()
    if flag in {"false", "0"} and label == "khuyến mãi":
        raise ValueError("Conflicting gift source flags")
    return flag in {"true", "1"} or label == "khuyến mãi"


def enrich_product_config(client: MobiWorkClient, cfg: dict[str, Any]) -> dict[str, Any]:
    """Read the documented Product catalogue for brand and packaging-unit conversion."""
    cfg = json.loads(json.dumps(cfg))
    seen_pages, products = set(), {}
    expected, count = None, 0
    for page in range(1, 10_001):
        payload = client.get_json("https://openapi.mobiwork.vn/OpenAPI/V1/Product",
                                  {"page_size": 200, "page_number": page},
                                  operation_key="promotion_detail_products", request_number=page)
        total = _api_total(payload, "Product catalogue")
        if expected is None:
            expected = total
        elif total is not None and total != expected:
            raise ValueError("Product catalogue total changed")
        rows = _expect_object_list(payload, "data", "Product catalogue")
        signature = json.dumps(rows, sort_keys=True, ensure_ascii=False)
        if rows and signature in seen_pages:
            raise ValueError("Product catalogue repeated page")
        seen_pages.add(signature)
        count += len(rows)
        for row in rows:
            sku = text(row.get("ma_sp"))
            if not sku:
                # Some documented catalogue entries have null codes; they cannot map a Bill SKU.
                continue
            if sku in products and products[sku] != row:
                raise ValueError("Conflicting Product catalogue SKU")
            products[sku] = row
        if not rows or (expected is not None and count >= expected):
            break
    else:
        raise ValueError("Product catalogue pagination safety limit")
    if expected is not None and count != expected:
        raise ValueError("Product catalogue total mismatch")
    for sku, row in products.items():
        brand = text(row.get("nhan_hieu"))
        if brand:
            cfg.setdefault("products", {}).setdefault(sku, {}).setdefault("Brand", brand)
        large, small = text(row.get("dvt_chan")), text(row.get("dvt_le"))
        if large.casefold() in {"thùng", "két", "bình"} and small and small != large:
            try:
                factor = number(row.get("hsqd"))
            except ValueError:
                continue
            if factor > 0:
                cfg.setdefault("unit_conversions", {}).setdefault(f"{sku}|{small}",
                    {"target_unit": large, "factor": str(Decimal(1) / factor),
                     "source": "Product.dvt_chan/dvt_le/hsqd"})
    cfg["product_catalogue_count"] = len(products)
    return cfg


def build_report(detail: pd.DataFrame, cfg: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = detail.to_dict("records")
    programs: dict[str, set[str]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        order = text(row.get("ma_phieu"))
        line = text(row.get("stt"))
        if not order or not line:
            raise ValueError("Bill detail requires ma_phieu and stt")
        key = (order, line)
        if key in seen:
            raise ValueError("Duplicate bill line; refusing inflated totals")
        seen.add(key)
        codes = programs.setdefault(order, set())
        for entry in promotion_list(row.get("promotion")):
            name = text(entry.get("ten_khuyen_mai")) or text(entry.get("id"))
            if name:
                codes.add(program_code(name, cfg))
        name = text(row.get("ctkm")) or text(row.get("ctkmFull_ten_khuyen_mai"))
        if name:
            codes.add(program_code(name, cfg))
        elif text(row.get("ctkmFull_id")):
            codes.add(program_code(text(row["ctkmFull_id"]), cfg))

    identities: dict[str, set[str]] = {}
    for row in rows:
        identities.setdefault(text(row.get("ma_kh")), set()).add(text(row.get("ID_khachhang")))
    output, issues = [], []
    for row in rows:
        order = text(row["ma_phieu"])
        if not programs[order]:
            continue
        gift = is_gift(row)
        money = gift and text(row.get("_money_reward")).casefold() in {"true", "1"}
        if gift:
            row = dict(row)
            for field in ("ma_sp", "ten_sp", "so_luong", "ma_dvt", "ten_dvt"):
                if not text(row.get(field)):
                    row[field] = row.get(f"{field}_km")
        sku = text(row.get("ma_sp"))
        unit = text(row.get("ten_dvt")) or text(row.get("ma_dvt"))
        item = dict.fromkeys(COLUMNS, None)
        if not gift:
            item["Số lượng Khuyến mãi"] = 0
        key = {"Mã Đơn hàng": order, "Dòng nguồn": text(row["stt"])}

        def issue(field: str, reason: str, key=key, sku=sku, unit=unit, row=row) -> None:
            issues.append({**key, "Trường": field, "Lý do": reason,
                           "Mã SP nguồn": sku, "ĐVT nguồn": unit,
                           "Số lượng nguồn": row.get("so_luong"),
                           "Tên SP nguồn": text(row.get("ten_sp")),
                           "Kho/NPP": warehouse(row), "Mã NV": text(row.get("ma_nv_dat")),
                           "Tên Nhân viên": text(row.get("ten_nguoi_dat")),
                           "Mã Khách hàng": text(row.get("ma_kh")),
                           "Tên Khách hàng": text(row.get("ten_kh"))})

        if not sku:
            issue("Mã sản phẩm", "Thiếu mã sản phẩm nguồn")
        aliases = {"Route": "tuyen_code", "Tên Nhân viên": "ten_nguoi_dat",
                   "Mã NV": "ma_nv_dat", "Mã Khách hàng": "ma_kh", "Tên Khách hàng": "ten_kh",
                   "Địa chỉ": "dia_chi", "Số ĐT": "sdt", "Loại KH": "loai_kh"}
        for label, source in aliases.items():
            item[label] = text(row.get(source)) or None
        item["Mã Đơn hàng"] = order
        item["Ngày Đơn hàng"] = row.get("ngay_dat") if text(row.get("ngay_dat")) else row.get("ngay_ban_hang")
        if not text(item["Ngày Đơn hàng"]):
            issue("Ngày Đơn hàng", "Thiếu ngày đơn hàng nghiệp vụ")
            item["Ngày Đơn hàng"] = None
        else:
            try:
                item["Ngày Đơn hàng"] = pd.Timestamp(item["Ngày Đơn hàng"]).to_pydatetime()
                if item["Ngày Đơn hàng"].tzinfo is not None:
                    raise ValueError("Ngày bán hàng có timezone chưa quy đổi")
            except (ValueError, TypeError):
                issue("Ngày Đơn hàng", "Ngày nghiệp vụ không hợp lệ")
                item["Ngày Đơn hàng"] = None
        item["Mã CTKM"] = "; ".join(sorted(programs[order]))
        if gift:
            direct = text(row.get("ctkm")) or text(row.get("ctkmFull_ten_khuyen_mai"))
            direct = program_code(direct or text(row.get("ctkmFull_id")), cfg)
            item["Mã CTKM"] = direct or item["Mã CTKM"]
            if not direct:
                issue("Mã CTKM", "Hàng tặng thiếu liên kết CTKM trực tiếp")
        region = cfg.get("employee_regions", {}).get(employee_prefix(text(row.get("ma_nv_dat"))), {})
        item["Vùng"] = region.get("vung") or None
        # Joined by the customer's internal ID; a later change of customer code on DMS keeps
        # the ID, so the order keeps its historical code and still gets current metadata.
        current_customer = cfg.get("customer_catalogue", {}).get(text(row.get("ID_khachhang")), {})
        for label in ("Tỉnh", "Loại KH", "Route", "Tên Khách hàng", "Địa chỉ", "Số ĐT"):
            if not text(item[label]) and text(current_customer.get(label)):
                item[label] = current_customer[label]
        customer = cfg.get("customers", {}).get(text(row.get("ID_khachhang")), {})
        code_mapping = cfg.get("customer_codes", {}).get(text(row.get("ma_kh")), {})
        if code_mapping:
            if len(identities[text(row.get("ma_kh"))]) != 1 or "" in identities[text(row.get("ma_kh"))]:
                issue("Mã Khách hàng", "Mã KH tham chiếu thiếu ID hoặc dùng bởi nhiều ID")
                code_mapping = {}
            elif code_mapping.get("employee_code") != text(row.get("ma_nv_dat")):
                issue("DB Code", "NVBH không khớp mapping tham chiếu; chưa xác định NPP")
                code_mapping = {}
        product = {**cfg.get("products", {}).get(sku, {}), **cfg.get("product_overrides", {}).get(sku, {})}
        layers = sales_layers(row, cfg)
        for mapping in (layers[0], layers[1], code_mapping, customer,
                        cfg.get("customer_overrides", {}).get(text(row.get("ma_kh")), {}), layers[2], product):
            for label, value in mapping.items():
                if not text(value):
                    continue
                if label in {"Vùng", "Tỉnh", "SS Code", "SS Name", "DB Code", "Tên NPP", "Brand", "Package", "Loại KH"}:
                    item[label] = value
        for label in ["Vùng", "Tỉnh", "SS Code", "SS Name", "DB Code", "Tên NPP", "Brand", "Package", "Loại KH"]:
            if money and label in {"Brand", "Package"}:
                continue  # cash / voucher reward: no product master data
            if not text(item[label]):
                issue(label, "Thiếu mapping danh mục")
        quantity_field = "Số lượng Khuyến mãi" if gift else "Số lượng SELL-OUT"
        try:
            quantity = number(row.get("so_luong"))
            conversion = cfg.get("unit_conversions", {}).get(f"{sku}|{unit}")
            if money or unit.casefold() in {"thùng", "két", "bình"} or (unit.casefold() == "cái" and not conversion):
                # Items counted per piece (vật phẩm) stay in CÁI, as the template header states.
                converted = quantity
            else:
                if not isinstance(conversion, dict) or conversion.get("target_unit", "").casefold() not in {"thùng", "két", "bình"}:
                    raise ValueError(UNIT_GAP + (" (đơn hàng thiếu ĐVT)" if not unit else ""))
                factor = number(conversion.get("factor"))
                if factor <= 0:
                    raise ValueError("Hệ số quy đổi phải dương")
                converted = quantity * factor
            item[quantity_field] = float(converted)
        except ValueError as exc:
            issue(quantity_field, str(exc))
        if gift:
            item["Sản phẩm Tặng"] = sku
            item["Tên Sản phẩm Tặng"] = text(row.get("ten_sp"))
        else:
            item["Mã sản phẩm"] = sku
            item["Tên Sản phẩm"] = text(row.get("ten_sp"))
            try:
                item["THÀNH TIỀN"] = float(number(row.get("so_luong")) * number(row.get("gia_truoc_vat")))
            except ValueError as exc:
                issue("THÀNH TIỀN", str(exc))
        output.append(item)
    report = _frame(output, "BaoCao").reindex(columns=COLUMNS)
    return report, _frame(issues, "CanBoSung")


OPTIONAL_MAPPING_FIELDS = frozenset({"Vùng", "Tỉnh", "SS Code", "SS Name", "DB Code", "Tên NPP",
                                     "Brand", "Package", "Loại KH"})


def blocking_issue_count(issues: pd.DataFrame) -> int:
    return len(blocking_issue_rows(issues))


def blocking_issue_rows(issues: pd.DataFrame) -> pd.DataFrame:
    """Missing mappings remain visible; invalid source values block publication."""
    if issues.empty:
        return issues
    return issues[~issues["Trường"].isin(OPTIONAL_MAPPING_FIELDS)
                  & ~issues["Lý do"].astype(str).str.startswith(UNIT_GAP)]


def unit_trace(detail: pd.DataFrame, report: pd.DataFrame, cfg: dict[str, Any]) -> pd.DataFrame:
    orders = set(report["Mã Đơn hàng"])
    result = []
    for row in detail.to_dict("records"):
        if text(row.get("ma_phieu")) not in orders:
            continue
        gift = is_gift(row)
        sku = text(row.get("ma_sp")) or (text(row.get("ma_sp_km")) if gift else "")
        unit = text(row.get("ten_dvt")) or text(row.get("ma_dvt"))
        if not unit and gift:
            unit = text(row.get("ten_dvt_km")) or text(row.get("ma_dvt_km"))
        conversion = cfg.get("unit_conversions", {}).get(f"{sku}|{unit}", {})
        target = unit if unit.casefold() in {"thùng", "két", "bình"} or (gift and unit.casefold() == "cái") else conversion.get("target_unit")
        result.append({"Mã Đơn hàng": text(row.get("ma_phieu")), "Dòng nguồn": text(row.get("stt")),
                       "Mã sản phẩm": sku, "Hàng tặng": gift, "ĐVT nguồn": unit,
                       "ĐVT báo cáo": target, "Hệ số": conversion.get("factor", 1 if target == unit else None)})
    return _frame(result, "DonViTinh")


def run() -> dict[str, Any]:
    dry = os.environ.get("DRY_RUN", "false").casefold() == "true"
    manifest: dict[str, Any] = {"dataset": "promotion_detail", "dry_run": dry,
                               "status": "running", "results": []}
    manifest_path = Path("output/promotion_detail_manifest.json")
    try:
        cfg = load_config()
        if os.environ.get("PUBLISH_PROMOTION_DETAIL", "false").casefold() == "true":
            cfg["publish_enabled"] = True
        allow_incomplete = (cfg.get("allow_incomplete_publish", False)
                            or os.environ.get("ALLOW_INCOMPLETE_DETAIL", "false").casefold() == "true")
        manifest["allow_incomplete_publish"] = allow_incomplete
        reports = load_reports(ROOT / "config/reports.json")
        bill = next(r for r in reports if r.key == "bill" and r.enabled)
        anchors = month_anchors(incremental_target_dates(os.environ.get("SYNC_SCOPE", "today"),
                                                       int(os.environ.get("LOOKBACK_DAYS", "1"))))
        scope = os.environ.get("PROMOTION_DETAIL_SCOPE", "touched")
        if scope not in {"touched", "all_existing"}:
            raise ValueError("PROMOTION_DETAIL_SCOPE must be touched or all_existing")
        manifest["scope"] = scope
        sharepoint, drive = None, ""
        if not dry or scope == "all_existing":
            sharepoint = SemanticSharePointClient.from_env()
            drive = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
            if not drive:
                drive = sharepoint.get_drive_id(sharepoint.get_site_id())
        if scope == "all_existing":
            anchors = discover_bill_months(sharepoint, drive, bill,
                                          datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date())
        manifest["source_months"] = [anchor.strftime("%Y-%m") for anchor in anchors]
        if cfg.get("fetch_program_catalogue", False):
            cfg = enrich_program_config(MobiWorkClient.from_env(), cfg)
            manifest["program_catalogue_count"] = cfg["program_catalogue_count"]
        if cfg.get("fetch_product_catalogue", False):
            cfg = enrich_product_config(MobiWorkClient.from_env(), cfg)
            manifest["product_catalogue_count"] = cfg["product_catalogue_count"]
        if cfg.get("fetch_customer_catalogue", False):
            cfg = enrich_customer_config(MobiWorkClient.from_env(), cfg)
            manifest["customer_catalogue"] = cfg["customer_catalogue_audit"]
        if cfg.get("fetch_sales_structure", False):
            try:  # SS / NPP / Vùng from the DMS department tree; explicit mappings win
                cfg = enrich_employee_config(MobiWorkClient.from_env(), cfg)
                manifest["sales_structure"] = cfg["sales_structure_audit"]
            except Exception as exc:
                manifest["sales_structure_error"] = f"{type(exc).__name__}: {exc}"
        use_bosung = cfg.get("bosung_mapping", False)
        if use_bosung and sharepoint is not None:
            cfg = bosung.apply_overrides(cfg, bosung.load_overrides(sharepoint, drive))
            manifest["bosung_overrides"] = cfg["bosung_override_counts"]
        prepared = []
        todo_updates: dict[str, list[dict[str, Any]]] = {}
        for anchor in anchors:
            if dry and scope == "touched":
                source = Path("output") / master_filename(bill.name, anchor)
                content = source.read_bytes()
            else:
                content = sharepoint.download_file_bytes(drive, _monthly_master_path(bill, anchor))
                if not content:
                    raise ValueError("Required bill monthly master missing")
            # dtype=object preserves identifiers and source cell values.
            detail = pd.read_excel(BytesIO(content), sheet_name="ChiTietSP", dtype=object)
            source_rows = len(detail)
            detail, outside_month = select_order_month(detail, anchor)
            report, issues = build_report(detail, cfg)
            path = write_detail_workbook({"BaoCao": report, "CanBoSung": issues,
                                   "DonViTinh": unit_trace(detail, report, cfg)},
                                  f"BaoCaoChiTietCTKM_{anchor:%Y-%m}.xlsx", anchor)
            result = {"month": f"{anchor:%Y-%m}", "rows": len(report), "issues": len(issues),
                      "filename": path.name, "source_scope": "requested_dates" if dry and scope == "touched" else "monthly_master",
                      "source_rows": source_rows, "outside_order_month_rows": outside_month,
                      "source_sha256": hashlib.sha256(content).hexdigest()}
            blocking = blocking_issue_rows(issues)
            result["blocking_issues"] = int(len(blocking))
            result["unit_gaps"] = 0 if issues.empty else int(issues["Lý do"].astype(str).str.startswith(UNIT_GAP).sum())
            if result["blocking_issues"]:
                reasons = (blocking["Trường"].astype(str) + ": " + blocking["Lý do"].astype(str)).value_counts()
                result["blocking_reasons"] = {k: int(v) for k, v in reasons.head(10).items()}
                result["blocking_samples"] = [
                    {k: str(row.get(k, "")) for k in ("Trường", "Lý do", "Mã SP nguồn", "ĐVT nguồn")}
                    for row in blocking.head(5).to_dict("records")]
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    print(f"::warning title=CTKM blocking issues {anchor:%m/%Y}::"
                          f"{json.dumps(result['blocking_reasons'], ensure_ascii=False)} "
                          f"samples={json.dumps(result['blocking_samples'], ensure_ascii=False)}")
            manifest["results"].append(result)
            prepared.append((anchor, path, result))
            label = f"CTKM {anchor:%Y-%m}"
            todo_updates[label] = bosung.aggregate(bosung.todo_rows(issues, cfg), label)
        if use_bosung:  # also when the month is blocked: the list says what to fix on DMS
            manifest["bosung"] = bosung.publish(todo_updates, sharepoint, drive, dry_run=dry)
        incomplete = any(result["issues"] for _, _, result in prepared)
        if not dry and cfg.get("publish_enabled", False) and any(result["blocking_issues"] for _, _, result in prepared):
            raise ValueError("CTKM report has invalid source values or identity links. Nothing published.")
        if incomplete and not dry and cfg.get("publish_enabled", False) and not allow_incomplete:
            raise ValueError("CTKM report needs mappings; see CanBoSung sheet. Nothing published.")
        if not dry and cfg.get("publish_enabled", False):
            for anchor, path, result in prepared:
                uploaded = sharepoint.upload_file(drive, path, f"07_BaoCaoChiTietCTKM/{anchor:%Y}/{anchor:%m}")
                result["upload_skipped"] = bool(uploaded.get("upload_skipped"))
                result["workbook_published"] = True
                result["remote_path"] = f"07_BaoCaoChiTietCTKM/{anchor:%Y}/{anchor:%m}/{path.name}"
        manifest["publish_enabled"] = bool(cfg.get("publish_enabled", False))
        manifest["status"] = "needs_mapping" if incomplete else "success"
        if incomplete and not dry and cfg.get("publish_enabled", False):
            manifest["status"] = "published_with_issues"
        return manifest
    except Exception as exc:
        manifest.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            detail = " ".join(f"{type(exc).__name__}: {exc}".split())[:500]
            print(f"::error title=Promotion detail failed::{detail}")
        raise
    finally:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
