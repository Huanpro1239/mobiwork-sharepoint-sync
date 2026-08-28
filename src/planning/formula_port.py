from __future__ import annotations

import calendar
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .normalize import clean_text, normalize_code, remove_vietnamese_accents, to_number

Row = Mapping[str, Any]


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _add_months(value: date, months: int) -> date:
    idx = value.year * 12 + value.month - 1 + months
    year, month0 = divmod(idx, 12)
    return date(year, month0 + 1, 1)


def _month_end(value: date) -> date:
    return date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])


def _risk_cap(value: Any) -> int:
    text = remove_vietnamese_accents(value).casefold()
    if "cao" in text:
        return 1
    if "tb" in text or "trung" in text:
        return 2
    return 3


def _roundup_excel(value: float) -> int:
    return math.ceil(value) if value >= 0 else -math.ceil(abs(value))


def _ceil_to_moq(value: float, moq: float) -> float:
    if value <= 0:
        return 0.0
    if moq <= 0:
        return value
    return math.ceil(value / moq) * moq


def forecast_by_month(
    fc_rows: Iterable[Row],
    month: int,
    *,
    code_column: str = "B",
) -> dict[str, float]:
    if month < 1 or month > 12:
        raise ValueError(f"month must be in 1..12, got {month}")
    value_column = chr(ord("D") + month)
    result: dict[str, float] = {}
    for row in fc_rows:
        code = normalize_code(row.get(code_column))
        if code and code not in result:
            result[code] = to_number(row.get(value_column))
    return result


def build_finished_goods_projection(
    product_codes: Iterable[Any],
    *,
    stock_vikoda: Mapping[str, float],
    stock_vkd: Mapping[str, float],
    plant_stock: Mapping[str, float],
    actual_sales: Mapping[str, float],
    forecast_current: Mapping[str, float],
    forecast_m1: Mapping[str, float],
    forecast_m2: Mapping[str, float],
    forecast_m3: Mapping[str, float],
    warehouse_debt: Mapping[str, float],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in product_codes:
        code = normalize_code(raw)
        d = to_number(stock_vikoda.get(code))
        e = to_number(stock_vkd.get(code))
        f = to_number(plant_stock.get(code))
        g = d + e - f
        h = to_number(actual_sales.get(code))
        i = to_number(forecast_current.get(code))
        j = i - h - f
        k = to_number(warehouse_debt.get(code))
        material_projection = j + k if j > 0 else k
        output.append(
            {
                "Ma SP": code,
                "D Ton Vikoda": d,
                "E Ton VKD": e,
                "F Ton Nha may": f,
                "G Ton cac kho khac": g,
                "H Da ban": h,
                "I FC": i,
                "J Con lai": j,
                "K No kho": k,
                "L Du kien vat tu": material_projection,
                "M FC M+1": to_number(forecast_m1.get(code)),
                "N FC M+2": to_number(forecast_m2.get(code)),
                "O FC M+3": to_number(forecast_m3.get(code)),
            }
        )
    return output


def standardize_flat_bom(rows: Iterable[Row]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        product = normalize_code(row.get("A"))
        material = normalize_code(row.get("B"))
        if not product or not material:
            continue
        output.append(
            {
                "product_code": product,
                "material_code": material,
                "material_name": clean_text(row.get("C")),
                "qty_per_product": to_number(row.get("D")),
            }
        )
    return output


def standardize_direct_bom(rows: Iterable[Row]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        product = normalize_code(row.get("A"))
        material = normalize_code(row.get("C"))
        if not product or not material:
            continue
        output.append(
            {
                "product_code": product,
                "product_name": clean_text(row.get("B")),
                "material_code": material,
                "material_name": clean_text(row.get("D")),
                "qty": to_number(row.get("E")),
            }
        )
    return output


def material_demand_periods(
    projection_rows: Iterable[Row],
    flat_bom_rows: Iterable[Row],
) -> dict[str, dict[str, float]]:
    projection = {normalize_code(row.get("Ma SP")): row for row in projection_rows}
    result: dict[str, dict[str, float]] = defaultdict(
        lambda: {"E": 0.0, "F": 0.0, "G": 0.0, "H": 0.0}
    )
    for row in flat_bom_rows:
        product = normalize_code(row.get("product_code"))
        material = normalize_code(row.get("material_code"))
        qty = to_number(row.get("qty_per_product"))
        src = projection.get(product)
        if not material or not src:
            continue
        result[material]["E"] += max(to_number(src.get("J Con lai")), 0.0) * qty
        result[material]["F"] += max(to_number(src.get("M FC M+1")), 0.0) * qty
        result[material]["G"] += max(to_number(src.get("N FC M+2")), 0.0) * qty
        result[material]["H"] += max(to_number(src.get("O FC M+3")), 0.0) * qty
    return dict(result)


def material_direct_projection(
    projection_rows: Iterable[Row],
    direct_bom_rows: Iterable[Row],
) -> tuple[dict[str, float], dict[str, float]]:
    projection = {normalize_code(row.get("Ma SP")): row for row in projection_rows}
    run_need: dict[str, float] = defaultdict(float)
    debt: dict[str, float] = defaultdict(float)
    for row in direct_bom_rows:
        product = normalize_code(row.get("product_code"))
        material = normalize_code(row.get("material_code"))
        qty = to_number(row.get("qty"))
        src = projection.get(product)
        if not material or not src:
            continue
        run_need[material] += to_number(src.get("L Du kien vat tu")) * qty
        debt[material] += to_number(src.get("K No kho")) * qty
    return dict(run_need), dict(debt)


def aggregate_open_po(
    po_rows: Iterable[Row],
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    totals: dict[str, float] = defaultdict(float)
    normalized: list[dict[str, Any]] = []
    for row in po_rows:
        code = normalize_code(row.get("Ma Hang"))
        if not code:
            continue
        remaining = max(
            to_number(row.get("So Luong mua")) - to_number(row.get("So Luong nhan")),
            0.0,
        )
        totals[code] += remaining
        normalized.append(
            {
                "material_code": code,
                "remaining": remaining,
                "delivery_date": _date_value(row.get("Ngay Giao")),
            }
        )
    return dict(totals), normalized


def build_material_inbound_plan(
    material_rows: Iterable[Row],
    *,
    stock: Mapping[str, float],
    direct_run_need: Mapping[str, float],
    open_po: Mapping[str, float],
    material_debt: Mapping[str, float],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in material_rows:
        code = normalize_code(row.get("A"))
        if not code:
            continue
        current_stock = to_number(stock.get(code))
        run_need = to_number(direct_run_need.get(code))
        po_qty = max(to_number(open_po.get(code)), 0.0)
        debt = to_number(material_debt.get(code))
        output.append(
            {
                "Ma NVL": code,
                "Ten NVL": clean_text(row.get("B")),
                "DVT": clean_text(row.get("C")),
                "D Ton thuc te": current_stock,
                "E Vat tu cho chay so": run_need,
                "F Con thieu": max(run_need - current_stock, 0.0),
                "G Ton PO": po_qty,
                "H No kho": debt,
                "I Con thieu no kho": current_stock - debt,
            }
        )
    return output


def shortage_date(
    *,
    today: date,
    stock: float,
    open_po: float,
    debt: float,
    demand_current: float,
    demand_m1: float,
    demand_m2: float,
    demand_m3: float,
) -> date | None:
    avail = to_number(stock) + to_number(open_po) - to_number(debt)
    demands = [
        max(to_number(demand_current), 0.0),
        max(to_number(demand_m1), 0.0),
        max(to_number(demand_m2), 0.0),
        max(to_number(demand_m3), 0.0),
    ]
    if sum(demands) <= 0:
        return today if avail < 0 else None
    if avail <= 0:
        return today

    days_current = max((_month_end(today) - today).days + 1, 1)
    if demands[0] > 0 and avail < demands[0]:
        return today + timedelta(days=int(avail / demands[0] * days_current))

    remaining = avail - demands[0]
    for offset, demand in enumerate(demands[1:], start=1):
        start = _add_months(today, offset)
        if remaining < demand:
            days_in_month = calendar.monthrange(start.year, start.month)[1]
            proportional = int(remaining / demand * days_in_month) if demand > 0 else 0
            return start + timedelta(days=proportional)
        remaining -= demand
    return None


def _abc_class(demand: float, all_demands: Sequence[float]) -> str:
    if demand <= 0:
        return "-"
    total = sum(all_demands)
    if total <= 0:
        return "-"
    cumulative = sum(value for value in all_demands if value > demand) + demand
    share = cumulative / total
    if share <= 0.8:
        return "A"
    if share <= 0.95:
        return "B"
    return "C"


def abc_risk(
    demand_3m: float,
    abc: str,
    leadtime: float,
    material_type: str,
    available: float,
) -> str:
    if demand_3m <= 0:
        return "⚪ Không NC"
    cover = available / demand_3m if demand_3m else 0.0
    if cover >= 1.3:
        return "🔴 Cao - đang dư"
    if abc == "C" or clean_text(material_type) == "Hương liệu":
        return "🟡 TB - C/LT dài" if leadtime >= 30 else "🔴 Cao - C/nhạy hạn"
    if abc == "B":
        return "🟡 Trung bình"
    return "🟢 Thấp"


def abc_feasibility(demand_3m: float, gap: float, leadtime: float) -> str:
    if demand_3m <= 0:
        return "⚪ Không có NC"
    if gap >= 0:
        return "✅ Đủ cho 3 tháng"
    ratio = abs(gap) / demand_3m
    if ratio > 0.5:
        return "🔴 Thiếu nhiều + LT dài" if leadtime >= 15 else "🔴 Thiếu nhiều"
    if ratio > 0.2:
        return "🟠 Thiếu vừa"
    return "🟡 Thiếu nhẹ"


def abc_cycle(
    demand_3m: float,
    gap: float,
    abc: str,
    leadtime: float,
    risk: str,
    available: float,
) -> str:
    if demand_3m <= 0:
        return "-"
    cover = available / demand_3m if demand_3m else 0.0
    cap = _risk_cap(risk)
    if cover >= 1.3:
        return "1 tháng (đã dư)"
    if gap < 0:
        if leadtime >= 30:
            return "2 tháng / chia nhịp" if cap >= 2 else "1 tháng / chia nhịp"
        return "1 tháng / nhịp gần"
    if abc == "A":
        if leadtime >= 15:
            return "2 tháng (chia nhịp)" if cap >= 2 else "1 tháng"
        return "1 tháng"
    if abc == "B":
        return "2 tháng" if leadtime >= 10 else "1 tháng"
    return "2 tháng có kiểm tra HSD" if cap >= 2 else "1 tháng để tránh tồn/HSD"


def build_abc_rows(
    material_rows: Iterable[Row],
    *,
    demand_periods: Mapping[str, Mapping[str, float]],
    stock: Mapping[str, float],
    open_po: Mapping[str, float],
    debt: Mapping[str, float],
) -> list[dict[str, Any]]:
    source = [row for row in material_rows if normalize_code(row.get("A"))]
    demand_3m_by_code = {
        normalize_code(row.get("A")): sum(
            max(
                to_number(
                    demand_periods.get(normalize_code(row.get("A")), {}).get(period)
                ),
                0.0,
            )
            for period in ("F", "G", "H")
        )
        for row in source
    }
    all_demands = list(demand_3m_by_code.values())
    output: list[dict[str, Any]] = []
    total = sum(all_demands)
    for row in source:
        code = normalize_code(row.get("A"))
        demand_3m = demand_3m_by_code[code]
        leadtime = to_number(row.get("E"))
        moq = to_number(row.get("D"))
        material_type = clean_text(row.get("F"))
        abc = _abc_class(demand_3m, all_demands)
        available = (
            to_number(stock.get(code))
            + to_number(open_po.get(code))
            - to_number(debt.get(code))
        )
        gap = available - demand_3m
        risk = abc_risk(demand_3m, abc, leadtime, material_type, available)
        feasibility = abc_feasibility(demand_3m, gap, leadtime)
        cycle = abc_cycle(demand_3m, gap, abc, leadtime, risk, available)
        share = demand_3m / total if total > 0 else 0.0
        if demand_3m <= 0:
            reason = "Không có NC 3T"
        elif demand_3m and available / demand_3m >= 1.3:
            reason = f"Đang dư {available / demand_3m:.0%} NC 3T; {risk} → chỉ mua nhỏ giọt/1T."
        elif gap < 0:
            reason = (
                f"Thiếu {abs(gap):,.0f}; LT {leadtime:g}d; {risk} "
                f"→ đặt theo nhịp gần, kiểm tra MOQ {moq:,.0f}"
            )
        elif "Cao" in risk:
            reason = "Đủ 3T nhưng rủi ro tồn/HSD cao → hạn chế mua thêm, ưu tiên 1T."
        else:
            reason = f"Đủ 3T; {abc} / {risk} → mua theo chu kỳ đề xuất."
        output.append(
            {
                "Ma NVL": code,
                "Ten NVL": clean_text(row.get("B")),
                "DVT": clean_text(row.get("C")),
                "Loai": material_type,
                "Leadtime": leadtime,
                "MOQ": moq,
                "G NC 3 thang": demand_3m,
                "H % NC rieng": share,
                "I ABC": abc,
                "J Ton + PO": available,
                "K Gap 3T": gap,
                "L Kha thi": feasibility,
                "M Chu ky mua de xuat": cycle,
                "N Ly do/Hanh dong": reason,
                "O Rui ro ton/HSD": risk,
            }
        )
    return output


def purchase_quantity(
    *,
    abc: str,
    risk: str,
    leadtime: float,
    moq: float,
    available: float,
    demand_current: float,
    demand_m1: float,
    demand_m2: float,
    demand_m3: float,
    days_to_shortage: int,
) -> float:
    cur = max(to_number(demand_current), 0.0)
    f = max(to_number(demand_m1), 0.0)
    g = max(to_number(demand_m2), 0.0)
    h = max(to_number(demand_m3), 0.0)
    q1 = f
    q2 = f + g
    q3 = f + g + h
    total = cur + q3
    if abc == "A":
        base = 3 if leadtime >= 45 else (2 if leadtime >= 15 else 1)
    elif abc == "B":
        base = 2 if leadtime >= 10 else 1
    else:
        base = 2 if leadtime >= 30 else 1
    cap = _risk_cap(risk)
    if total <= 0 or q3 <= 0:
        cycle = 0
    elif days_to_shortage <= 14:
        urgent_base = max(2, base) if leadtime >= 30 else max(1, base)
        cycle = min(cap, urgent_base)
    else:
        cycle = min(cap, base)
    targets = {0: 0.0, 1: q1, 2: q2, 3: q3}
    target = targets[cycle]
    factor = 1.0 if cycle == 1 else (1.05 if leadtime >= 30 else 1.0)
    need = max(0.0, target * factor - max(0.0, available))
    urgent = (
        max(0.0, cur + q1 - max(0.0, available))
        if days_to_shortage <= 14
        else 0.0
    )
    return _ceil_to_moq(max(need, urgent), moq)


def purchase_status(
    *,
    total_demand: float,
    debt: float,
    available: float,
    shortage: date | None,
    today: date,
    abc: str,
    open_po: float,
    demand_current: float,
    demand_m1: float,
) -> str:
    days = 999 if shortage is None else (shortage - today).days
    if total_demand == 0 and debt == 0:
        return "⚪ KHÔNG NC"
    if days > 60 and available >= (demand_current + demand_m1) * 1.3:
        return "✅ ĐỦ DƯ"
    if shortage is None:
        return "✅ ĐỦ HÀNG"
    if days <= 0:
        return "🔴 NGUY CẤP - QUÁ HẠN"
    if days <= 7:
        return "🔴 NGUY CẤP"
    if days <= 14:
        return "🟠 KHẨN CẤP (A)" if abc == "A" else "🟠 KHẨN CẤP"
    if days <= 30:
        return "🟤 CẦN MUA" if open_po > 0 else "🟡 CẦN MUA GẤP"
    if days <= 60:
        return "🔵 LÊN KẾ HOẠCH" if open_po > 0 else "🟤 CẦN MUA"
    return "🔵 LÊN KẾ HOẠCH"


def purchase_action(status: str, open_po: float) -> str:
    if status in {"⚪ KHÔNG NC", "✅ KHÔNG NC", "✅ ĐỦ HÀNG", "✅ ĐỦ DƯ"}:
        return "Không cần hành động"
    if status == "🔴 NGUY CẤP - QUÁ HẠN":
        return "ĐẶT HÀNG KHẨN CẤP - Đã quá hạn thiếu hàng!"
    if status == "🔴 NGUY CẤP":
        return "Đặt hàng KHẨN - Thiếu ngay trong 7 ngày tới!"
    if status in {"🟠 KHẨN CẤP", "🟠 KHẨN CẤP (A)"}:
        return "Đặt hàng ngay - Thiếu trong 8-14 ngày"
    if status == "🟡 CẦN MUA GẤP":
        return (
            "Có PO nhưng chưa đủ - Bổ sung tuần này"
            if open_po > 0
            else "Chưa có PO - Đặt hàng tuần này"
        )
    if status == "🟤 CẦN MUA":
        return (
            "Có PO, cần bổ sung thêm trong 2 tuần"
            if open_po > 0
            else "Lên PO trong 2 tuần tới"
        )
    if status == "🔵 LÊN KẾ HOẠCH":
        return (
            "Có PO - Lên KH bổ sung tháng tới"
            if open_po > 0
            else "Đưa vào KH mua tháng tới"
        )
    if status == "🔻 TỒN THẤP":
        return "Tồn+PO thấp, cần bổ sung sớm"
    if status == "📋 THEO DÕI PO":
        return "Tồn+PO đủ nhưng sát - Theo dõi tiến độ giao PO"
    return "Không cần hành động"


def purchase_priority(status: str) -> int:
    if status in {"🔴 NGUY CẤP", "🔴 NGUY CẤP - QUÁ HẠN"}:
        return 1
    if status in {"🟠 KHẨN CẤP", "🟠 KHẨN CẤP (A)"}:
        return 2
    if status in {"🟡 CẦN MUA GẤP", "🔻 TỒN THẤP"}:
        return 3
    if status == "🟤 CẦN MUA":
        return 4
    if status == "🔵 LÊN KẾ HOẠCH":
        return 5
    if status == "📋 THEO DÕI PO":
        return 6
    return 9


def previous_workday(value: date, holidays: set[date]) -> date:
    current = value
    while current.weekday() >= 5 or current in holidays:
        current -= timedelta(days=1)
    return current


def next_workday(value: date, holidays: set[date]) -> date:
    current = value
    while current.weekday() >= 5 or current in holidays:
        current += timedelta(days=1)
    return current


def purchase_dates(
    *,
    today: date,
    shortage: date | None,
    leadtime: float,
    suggested_qty: float,
    holidays: set[date],
) -> tuple[date | None, date | None]:
    if suggested_qty <= 0:
        return None, None
    need_date = shortage or _month_end(_add_months(today, 1))
    raw_order = need_date - timedelta(days=int(leadtime))
    purchase_date = today if raw_order <= today else previous_workday(raw_order, holidays)
    batch = date(
        purchase_date.year,
        purchase_date.month,
        5 if purchase_date.day <= 10 else 15,
    )
    order_date = today if batch <= today else next_workday(batch, holidays)
    return purchase_date, order_date


def build_purchase_plan(
    material_rows: Iterable[Row],
    *,
    demand_periods: Mapping[str, Mapping[str, float]],
    stock: Mapping[str, float],
    open_po: Mapping[str, float],
    debt: Mapping[str, float],
    abc_rows: Iterable[Row],
    today: date,
    holidays: set[date],
) -> list[dict[str, Any]]:
    abc_map = {normalize_code(row.get("Ma NVL")): row for row in abc_rows}
    output: list[dict[str, Any]] = []
    for row in material_rows:
        code = normalize_code(row.get("A"))
        if not code:
            continue
        periods = demand_periods.get(code, {})
        e = max(to_number(periods.get("E")), 0.0)
        f = max(to_number(periods.get("F")), 0.0)
        g = max(to_number(periods.get("G")), 0.0)
        h = max(to_number(periods.get("H")), 0.0)
        total = e + f + g + h
        d = to_number(stock.get(code))
        j = to_number(open_po.get(code))
        k = to_number(debt.get(code))
        available = d + j - k
        shortage = shortage_date(
            today=today,
            stock=d,
            open_po=j,
            debt=k,
            demand_current=e,
            demand_m1=f,
            demand_m2=g,
            demand_m3=h,
        )
        days = 999 if shortage is None else (shortage - today).days
        abc_row = abc_map.get(code, {})
        abc = clean_text(abc_row.get("I ABC")) or "C"
        risk = clean_text(abc_row.get("O Rui ro ton/HSD")) or "🔴 Cao"
        leadtime = to_number(row.get("E"), 7.0)
        moq = to_number(row.get("D"))
        suggested = purchase_quantity(
            abc=abc,
            risk=risk,
            leadtime=leadtime,
            moq=moq,
            available=available,
            demand_current=e,
            demand_m1=f,
            demand_m2=g,
            demand_m3=h,
            days_to_shortage=days,
        )
        status = purchase_status(
            total_demand=total,
            debt=k,
            available=available,
            shortage=shortage,
            today=today,
            abc=abc,
            open_po=j,
            demand_current=e,
            demand_m1=f,
        )
        cover: float | str
        if total == 0:
            cover = "-"
        elif d + j <= 0:
            cover = 0.0
        else:
            cover = (d + j) / total
        purchase_date, order_date = purchase_dates(
            today=today,
            shortage=shortage,
            leadtime=leadtime,
            suggested_qty=suggested,
            holidays=holidays,
        )
        note = ""
        if status not in {"⚪ KHÔNG NC", "✅ ĐỦ HÀNG", "✅ ĐỦ DƯ"}:
            shortage_text = shortage.strftime("%d/%m") if shortage else "-"
            note = (
                f"Tồn+PO: {d + j:,.0f} | NC: {total:,.0f} | "
                f"Thiếu {shortage_text} | LT:{leadtime:g}d"
            )
        output.append(
            {
                "Ma NVL": code,
                "Ten NVL": clean_text(row.get("B")),
                "DVT": clean_text(row.get("C")),
                "D Ton kho": d,
                "E NC hien tai": e,
                "F NC M+1": f,
                "G NC M+2": g,
                "H NC M+3": h,
                "I Tong NC": total,
                "J PO da mo": j,
                "K No kho": k,
                "L SL de xuat mua": suggested,
                "M Ton kha dung": available,
                "N Ngay thieu": shortage,
                "O Muc rui ro": status,
                "P Cover": cover,
                "Q De xuat hanh dong": purchase_action(status, j),
                "R Uu tien": purchase_priority(status),
                "S SL de xuat mua chuan": suggested,
                "T Ghi chu": note,
                "U Ngay mua hang": purchase_date,
                "V Ngay dat mua": order_date,
            }
        )
    return output


def build_fc_end_stock(
    product_codes: Iterable[Any],
    *,
    current_forecast: Mapping[str, float],
    gui_kho_begin: Mapping[str, float],
    leadtime: Mapping[str, float],
    working_days: float = 26.0,
) -> tuple[dict[str, float], dict[str, float]]:
    daily: dict[str, float] = {}
    end_stock: dict[str, float] = {}
    for raw in product_codes:
        code = normalize_code(raw)
        avg = to_number(current_forecast.get(code)) / working_days if working_days else 0.0
        minimum = avg * (to_number(leadtime.get(code)) + 2.0)
        daily[code] = avg
        end_stock[code] = (
            0.0 if to_number(gui_kho_begin.get(code)) > minimum else minimum
        )
    return daily, end_stock


def build_weekly_production_plan(
    config_rows: Iterable[Row],
    *,
    plan_month: int,
    plan_year: int,
    actual_stock: Mapping[str, float],
    opening_book_stock: Mapping[str, float],
    forecast: Mapping[str, float],
    projected_end_stock: Mapping[str, float],
    warehouse_debt: Mapping[str, float],
    daily_sales: Mapping[str, float],
    leadtime: Mapping[str, float],
) -> list[dict[str, Any]]:
    first = date(plan_year, plan_month, 1)
    output: list[dict[str, Any]] = []
    for source_row, row in enumerate(config_rows, start=4):
        code = normalize_code(row.get("A"))
        if not code:
            continue
        batch = to_number(row.get("D"))
        per_shift = to_number(row.get("E"))
        shifts_day = to_number(row.get("J"))
        opening_actual = to_number(actual_stock.get(code))
        opening_book = to_number(opening_book_stock.get(code))
        fc = to_number(forecast.get(code))
        end_stock = to_number(projected_end_stock.get(code))
        debt = to_number(warehouse_debt.get(code))
        need = fc + debt - opening_book if debt > 0 else fc + end_stock - opening_book + debt
        if need == 0:
            rounded = 0.0
        else:
            multiple = batch if clean_text(row.get("H")) == "Có đường" else per_shift
            rounded = _roundup_excel(need / multiple) * multiple if multiple else 0.0
        days = rounded / per_shift / shifts_day if per_shift and shifts_day else 0.0
        avg = to_number(daily_sales.get(code))
        start: date | None
        if avg <= 0:
            start = None
        else:
            candidate = first + timedelta(
                days=opening_actual / avg - to_number(leadtime.get(code))
            )
            start = candidate
            if start < first:
                start = first
        output.append(
            {
                "Source row": source_row,
                "Ma SP": code,
                "Ten SP": clean_text(row.get("B")),
                "DVT": clean_text(row.get("C")),
                "So luong/me": batch,
                "So luong/ca": per_shift,
                "Chuyen": clean_text(row.get("F")),
                "Nhom SP": clean_text(row.get("G")),
                "Phan loai SP": clean_text(row.get("H")),
                "Ton dau thuc te": opening_actual,
                "So ca/ngay": shifts_day,
                "Ton dau so sach": opening_book,
                "FC": fc,
                "Ton cuoi du kien": end_stock,
                "No kho": debt,
                "SL can san xuat": need,
                "SL SX tron me/ca": rounded,
                "So ngay can SX": days,
                "Ngay bat dau SX": start,
            }
        )
    return output


def _helper_two_line(
    rows: Iterable[Row], first: date
) -> dict[int, tuple[float, float]]:
    eligible = [
        row
        for row in rows
        if clean_text(row.get("Chuyen")) in {"PET 9000", "KHS"}
        and to_number(row.get("SL SX tron me/ca")) > 0
        and _date_value(row.get("Ngay bat dau SX")) is not None
    ]
    eligible.sort(
        key=lambda row: (
            clean_text(row.get("Chuyen")),
            _date_value(row.get("Ngay bat dau SX")) or date.max,
            int(to_number(row.get("Source row"))),
        )
    )
    previous_by_line: dict[str, tuple[Any, float]] = {}
    result: dict[int, tuple[float, float]] = {}
    for row in eligible:
        line = clean_text(row.get("Chuyen"))
        per_shift = to_number(row.get("So luong/ca"))
        shifts_day = to_number(row.get("So ca/ngay"))
        start_date = _date_value(row.get("Ngay bat dau SX"))
        earliest = ((start_date - first).days if start_date else 0) * shifts_day
        previous = previous_by_line.get(line)
        changeover = (
            0.5
            if previous is not None
            and to_number(previous[0]) != to_number(row.get("Ton dau thuc te"))
            else 0.0
        )
        previous_end = previous[1] if previous is not None else 0.0
        start_shift = max(earliest, previous_end) + changeover
        end_shift = (
            start_shift + to_number(row.get("SL SX tron me/ca")) / per_shift
            if per_shift
            else start_shift
        )
        source_row = int(to_number(row.get("Source row")))
        result[source_row] = (start_shift, end_shift)
        previous_by_line[line] = (row.get("Ton dau thuc te"), end_shift)
    return result


def build_algorithmic_daily_schedule(
    weekly_rows: Iterable[Row],
    *,
    plan_year: int,
    plan_month: int,
) -> list[dict[str, Any]]:
    rows = list(weekly_rows)
    first = date(plan_year, plan_month, 1)
    days_in_month = calendar.monthrange(plan_year, plan_month)[1]
    helper = _helper_two_line(rows, first)
    output: list[dict[str, Any]] = []

    for row in rows:
        q = to_number(row.get("SL SX tron me/ca"))
        start = _date_value(row.get("Ngay bat dau SX"))
        if q <= 0 or start is None:
            continue
        line = clean_text(row.get("Chuyen"))
        code = normalize_code(row.get("Ma SP"))
        per = to_number(row.get("So luong/ca"))
        spd = to_number(row.get("So ca/ngay"))
        source_row = int(to_number(row.get("Source row")))
        day_quantities: dict[date, float] = {}

        for day in range(1, days_in_month + 1):
            current = date(plan_year, plan_month, day)
            raw = 0.0
            if line in {"PET 9000", "KHS"}:
                ps, pe = helper.get(source_row, (0.0, 0.0))
                raw = (
                    max(0.0, min(pe, day * spd) - max(ps, (day - 1) * spd))
                    * per
                )
            elif line == "Galon" and code == "130100006":
                work_days = sum(
                    1
                    for d in range(1, days_in_month + 1)
                    if date(plan_year, plan_month, d).weekday() != 6
                )
                rank = sum(
                    1
                    for d in range(1, day + 1)
                    if date(plan_year, plan_month, d).weekday() != 6
                )
                base = q / work_days if q <= work_days * per else per
                extra_qty = max(0.0, q - work_days * per)
                if extra_qty:
                    extra_today = (
                        math.floor(extra_qty / per * rank / work_days)
                        - math.floor(extra_qty / per * (rank - 1) / work_days)
                    ) * per
                else:
                    extra_today = 0.0
                raw = 0.0 if current.weekday() == 6 else base + extra_today
            elif line == "Galon":
                start_day = (start - first).days + 1
                run_day = day - start_day
                raw = max(
                    0.0,
                    min(q, (run_day + 1) * spd * per)
                    - max(0.0, run_day * spd * per),
                )
            if raw > 0:
                day_quantities[current] = raw

        output.append(
            {
                "Ma SP": code,
                "Ten SP": row.get("Ten SP"),
                "DVT": row.get("DVT"),
                "Chuyen": line,
                "SL ke hoach": q,
                "Ngay bat dau SX": start,
                **{d.isoformat(): qty for d, qty in day_quantities.items()},
            }
        )
    return output


def build_daily_material_allocation(
    material_rows: Iterable[Row],
    *,
    flat_bom_rows: Iterable[Row],
    daily_product_rows: Iterable[Row],
    stock: Mapping[str, float],
    po_lines: Iterable[Row],
    start_date: date,
    horizon_days: int = 45,
) -> list[dict[str, Any]]:
    daily_rows = list(daily_product_rows)
    qty_by_product_date: dict[tuple[str, date], float] = defaultdict(float)
    all_dates = [start_date + timedelta(days=i) for i in range(horizon_days)]
    for row in daily_rows:
        product = normalize_code(row.get("Ma SP"))
        for current in all_dates:
            qty_by_product_date[(product, current)] += to_number(
                row.get(current.isoformat())
            )

    coeff: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for row in flat_bom_rows:
        material = normalize_code(row.get("material_code"))
        product = normalize_code(row.get("product_code"))
        if material and product:
            coeff[material].append(
                (product, to_number(row.get("qty_per_product")))
            )

    po_by_material: dict[str, list[tuple[date | None, float]]] = defaultdict(list)
    for row in po_lines:
        code = normalize_code(row.get("material_code"))
        if code:
            po_by_material[code].append(
                (_date_value(row.get("delivery_date")), to_number(row.get("remaining")))
            )

    output: list[dict[str, Any]] = []
    end_date = start_date + timedelta(days=horizon_days - 1)
    for material_row in material_rows:
        code = normalize_code(material_row.get("A"))
        if not code:
            continue
        demand_by_day: dict[date, float] = {}
        for current in all_dates:
            demand_by_day[current] = sum(
                qty_by_product_date.get((product, current), 0.0) * qty
                for product, qty in coeff.get(code, [])
            )
        total_demand = sum(demand_by_day.values())
        opening = to_number(stock.get(code))
        total_po = sum(max(qty, 0.0) for _, qty in po_by_material.get(code, []))
        period_po = sum(
            max(qty, 0.0)
            for delivery, qty in po_by_material.get(code, [])
            if delivery is not None and start_date <= delivery <= end_date
        )
        cumulative_demand = 0.0
        shortage: date | None = None
        for current in all_dates:
            cumulative_demand += demand_by_day[current]
            cumulative_po = sum(
                max(qty, 0.0)
                for delivery, qty in po_by_material.get(code, [])
                if delivery is not None and start_date <= delivery <= current
            )
            if cumulative_demand > opening + cumulative_po:
                shortage = current
                break
        if total_demand <= opening:
            status = "Du hang"
        elif opening + period_po >= total_demand:
            status = "Cho PO ve trong ky"
        elif opening + total_po >= total_demand:
            status = "PO co nhung can xem ngay giao"
        else:
            status = "Can dat mua them"
        note = {
            "Can dat mua them": "Can mua them de dap ung ke hoach SX trong ky.",
            "PO co nhung can xem ngay giao": (
                "PO du tong luong nhung chua co/khong du ngay giao trong ky."
            ),
            "Cho PO ve trong ky": "Da can doi theo Ngay Giao nam trong ky tren sheet PO.",
        }.get(status, "")
        output.append(
            {
                "Ma NVL": code,
                "Ten NVL": clean_text(material_row.get("B")),
                "DVT": clean_text(material_row.get("C")),
                "D Nhu cau tu ngay": total_demand,
                "E Ton dau": opening,
                "F PO mo": total_po,
                "G PO trong ky": period_po,
                "H Ngay thieu dau tien": shortage,
                "I Trang thai": status,
                "J Can mua them": max(total_demand - opening - total_po, 0.0),
                "K Ghi chu": note,
            }
        )
    return output
