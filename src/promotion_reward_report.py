"""Reward quantities, cash and observed invoice promotions at their own grains.

Calculated eligibility is not proof of payment. Invoice gifts are observed DMS
transactions; they are never added to calculated rewards as a second payout.
"""
from __future__ import annotations

from datetime import date, datetime
import json
from typing import Any

import pandas as pd

import promotion_bonus_calc as calc
from promotion_detail import build_report, is_gift, promotion_list
from promotion_models import ProgramResult, text
from promotion_months import select_order_month

REWARD_COLUMNS = ["Tháng", "Mã CT", "Tên CT", "ID CT", "Vùng", "Tỉnh", "Tên NPP",
                  "Mã Khách hàng", "Tên Khách hàng", "Loại thưởng", "Mã quà", "Tên quà", "ĐVT",
                  "Số lượng quà dự kiến", "Số lượng quà đủ điều kiện", "Tiền mặt dự kiến (đ)",
                  "Tiền mặt đủ điều kiện (đ)", "Trạng thái"]
PROGRAM_COLUMNS = ["Tháng", "Nguồn", "Mã CT", "Tên CT", "ID CT", "Từ ngày", "Đến ngày",
                   "Hình thức", "Loại thưởng", "Khách đăng ký", "Khách có doanh số", "Khách đạt",
                   "Khách đủ điều kiện", "Khách cần xem", "Số đơn DMS", "Trạng thái", "Lý do"]
INVOICE_COLUMNS = {"Dòng nguồn": "stt", "ĐVT nguồn": "_source_unit",
                   "Số lượng nguồn": "_source_quantity", "Chiết khấu SP nguồn DMS": "chiet_khau"}


def _type_label(value: Any) -> str:
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            value = json.loads(value)
        except ValueError:
            return text(value)
    return text(value)


def reward_ledger(results: list[ProgramResult], month: date) -> pd.DataFrame:
    """One customer × programme × reward, including zero/unconfirmed rewards."""
    records = []
    for result in results:
        try:
            rule = calc.parse_rule(result.program)
        except ValueError:
            continue  # catalogue coverage retains this programme and the reason
        for row in result.rows:
            proposed = row.get("objThuongDuKien") or {}
            confirmed = row.get("objTraThuong") or {}
            extra = row.get("extra") or {}
            for sku, name, unit, _ in rule.rewards:
                key = f"{sku}|{unit}"
                money = sku == calc.MONEY_SKU
                records.append({
                    "Tháng": month.strftime("%Y-%m"), "Mã CT": calc.bonus_code(result.program_name),
                    "Tên CT": result.program_name, "ID CT": result.program_id,
                    "Vùng": row.get("kv"), "Tỉnh": extra.get("Tỉnh"), "Tên NPP": row.get("npp"),
                    "Mã Khách hàng": row.get("ma"), "Tên Khách hàng": row.get("ten"),
                    "Loại thưởng": "Tiền mặt" if money else "Quà tặng",
                    "Mã quà": None if money else sku, "Tên quà": name or sku, "ĐVT": unit,
                    "Số lượng quà dự kiến": 0 if money else proposed.get(key, 0),
                    "Số lượng quà đủ điều kiện": 0 if money else confirmed.get(key, 0),
                    "Tiền mặt dự kiến (đ)": proposed.get(key, 0) if money else 0,
                    "Tiền mặt đủ điều kiện (đ)": confirmed.get(key, 0) if money else 0,
                    "Trạng thái": extra.get("Đủ điều kiện trả thưởng") or row.get("_eligible"),
                })
    return pd.DataFrame(records, columns=REWARD_COLUMNS)


def _day(value: Any) -> date | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, calc.VN_TZ).date()
    except (TypeError, ValueError, OSError):
        return None


def program_coverage(results: list[ProgramResult], month: date,
                     issues: list[dict[str, Any]]) -> pd.DataFrame:
    """Every selected catalogue entry, including unsupported and empty programmes."""
    reasons = {text(i.get("Mã CT (id)")): text(i.get("Vấn đề")) for i in issues}
    records = []
    for result in results:
        diag = calc.diagnostics([result])[0]
        statuses = {text(r.get("_eligible")) for r in result.rows} - {"Có", "Không", ""}
        reason = reasons.get(result.program_id, "")
        pending = sum(r.get("_eligible") not in {"Có", "Không"} for r in result.rows)
        try:
            rule = calc.parse_rule(result.program)
            kinds = {"Tiền mặt" if sku == calc.MONEY_SKU else "Quà tặng" for sku, _, _, _ in rule.rewards}
        except ValueError:
            kinds = set()
        records.append({
            "Tháng": month.strftime("%Y-%m"), "Nguồn": "Danh mục trả thưởng OpenAPI",
            "Mã CT": calc.bonus_code(result.program_name), "Tên CT": result.program_name,
            "ID CT": result.program_id, "Từ ngày": _day(result.program.get("startDate")),
            "Đến ngày": _day(result.program.get("endDate")), "Hình thức": _type_label(result.program.get("ptype")),
            "Loại thưởng": "; ".join(sorted(kinds)) or "Chưa xác định",
            "Khách đăng ký": len(set(result.program.get("customer") or [])),
            "Khách có doanh số": diag["with_sales"], "Khách đạt": diag["reached"],
            "Khách đủ điều kiện": diag["eligible"], "Khách cần xem": pending,
            "Số đơn DMS": None,
            "Trạng thái": ("Chưa tính được" if reason else "Cần xem" if statuses else
                           "Không có khách đăng ký" if not result.rows else "Đã tính"),
            "Lý do": reason or "; ".join(sorted(statuses)),
        })
    return pd.DataFrame(records, columns=PROGRAM_COLUMNS)


def invoice_promotions(detail: pd.DataFrame, cfg: dict[str, Any], month: date
                       ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Observed promotions only; sales once with all codes, gifts once as on Bill.

    Uses the order month, matching the standalone CTKM report. Reward eligibility
    uses the delivery month. Source discount is kept as supplied, not interpreted
    as cash reward or allocated from a repeated order-header value.
    """
    source, _ = select_order_month(detail, month)
    source = source.copy()
    source["_source_unit"] = [text(r.get("ten_dvt")) or text(r.get("ten_dvt_km"))
                              for r in source.to_dict("records")]
    source["_source_quantity"] = [r.get("so_luong") if text(r.get("so_luong")) else r.get("so_luong_km")
                                  for r in source.to_dict("records")]
    report, issues = build_report(source, cfg, INVOICE_COLUMNS)
    programmes: dict[str, dict[str, Any]] = {}
    for row in source.to_dict("records"):
        entries = [dict(e) for e in promotion_list(row.get("promotion"))]
        direct = text(row.get("ctkm")) or text(row.get("ctkmFull_ten_khuyen_mai"))
        direct_id = text(row.get("ctkmFull_id"))
        # An embedded entry's ID is authoritative; avoid a second row keyed by its name.
        if (direct or direct_id) and not any(direct_id == text(e.get("id")) if direct_id else
                                            direct == text(e.get("ten_khuyen_mai")) for e in entries):
            entries.append({"id": direct_id, "ten_khuyen_mai": direct})
        for entry in entries:
            name = text(entry.get("ten_khuyen_mai"))
            pid = text(entry.get("id"))
            if not pid and not name:
                continue
            item = programmes.setdefault(pid or name, {"name": name or pid, "id": pid,
                                                        "type": _type_label(entry.get("ptype")), "orders": set()})
            item["orders"].add(text(row.get("ma_phieu")))
    from promotion_detail import program_code

    coverage = pd.DataFrame([{
        "Tháng": month.strftime("%Y-%m"), "Nguồn": "Khuyến mãi trên đơn DMS",
        "Mã CT": program_code(p["name"], cfg), "Tên CT": p["name"], "ID CT": p["id"],
        "Hình thức": p["type"], "Loại thưởng": "Khuyến mãi trên đơn",
        "Số đơn DMS": len(p["orders"]), "Trạng thái": "Đã có trên đơn",
        "Lý do": "Theo Bill; không cộng lần nữa vào thưởng tính từ doanh số",
    } for p in programmes.values()], columns=PROGRAM_COLUMNS)
    # Plain booleans let the manifest count actual physical gift rows independently.
    reported_orders = {text(v) for v in report.get("Mã Đơn hàng", [])}
    report.attrs["gift_rows"] = sum(is_gift(r) for r in source.to_dict("records")
                                    if text(r.get("ma_phieu")) in reported_orders)
    return report, issues, coverage
