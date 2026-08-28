from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

from ..normalize import clean_text, normalize_code, remove_vietnamese_accents, to_number
from .common import Row, add_months, ceil_to_moq, month_end


def _risk_cap(value: Any) -> int:
    text = remove_vietnamese_accents(value).casefold()
    if "cao" in text:
        return 1
    if "tb" in text or "trung" in text:
        return 2
    return 3


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

    days_current = max((month_end(today) - today).days + 1, 1)
    if demands[0] > 0 and avail < demands[0]:
        return today + timedelta(days=int(avail / demands[0] * days_current))

    remaining = avail - demands[0]
    for offset, demand in enumerate(demands[1:], start=1):
        start = add_months(today, offset)
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
    return ceil_to_moq(max(need, urgent), moq)


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
    need_date = shortage or month_end(add_months(today, 1))
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
