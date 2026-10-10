"""Customer progress in the supplied accumulation template, using the bonus rules.

Monthly columns show sales thresholds reached, not evidence of cash/gift payment.
Current eligibility continues to be owned by promotion_bonus_calc.compute.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from calendar import monthrange
from typing import Any, Callable

import pandas as pd

import promotion_bonus_calc as calc
from promotion_detail import resolve_sales
from promotion_models import ProgramResult, text

BASE_COLUMNS = ["#", "Vùng", "NPP", "Tên SS", "Tên NVBH", "Route", "Mã KH", "Tên Điểm bán",
                "Tên người liên hệ", "Loại KH", "Địa chỉ", "Số điện thoại", "Tên CT", "NGÀY ĐĂNG KÝ",
                "MỨC", "MỤC TIÊU", "SỐ SUẤT", "MỤC TIÊU HOÀN THÀNH", "TỔNG TÍCH LŨY", "CÒN LẠI"]
EXTRA_COLUMNS = ["Mã CT", "ĐVT mục tiêu", "Cách tính", "Thực hiện kỳ xét thưởng", "Trạng thái trả thưởng",
                 "Thông tin nguồn"]
TITLE = "THEO DÕI SẢN LƯỢNG KHÁCH HÀNG THAM GIA TÍCH LŨY"


def _assigned(assignments: dict[str, set[str]], notes: set[str], field: str) -> str:
    candidates = assignments.get(field, set())
    if len(candidates) > 1:
        notes.add(f"Nhiều phân công {field}")
    return next(iter(candidates)) if len(candidates) == 1 else ""


def program_day(program: dict[str, Any], field: str) -> date | None:
    try:
        return datetime.fromtimestamp(int(program[field]) / 1000, calc.VN_TZ).date()
    except (KeyError, TypeError, ValueError, OSError):
        return None


def month_keys(first: date, last: date) -> list[date]:
    cursor, result = first.replace(day=1), []
    while cursor <= last:
        result.append(cursor)
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return result


def tracking_report(results: list[ProgramResult], first: date, last: date,
                    load_month: Callable[[date], pd.DataFrame | None], customers: dict[str, Any],
                    cfg: dict[str, Any], modes: dict[str, str], newest: date | None = None) -> pd.DataFrame:
    """One registered customer × programme level; never use example template rows.

The user explicitly confirmed one registration equals one slot. Missing registration
dates stay blank. Missing/future monthly masters stay blank, distinct from observed zero.
For monthly programmes CÒN LẠI refers to this month's target. For cumulative programmes
it refers to the whole-period target; achieved slots are increments of running attainment.
"""
    starts = [program_day(r.program, "startDate") or first for r in results]
    ends = [program_day(r.program, "endDate") or last for r in results]
    months = month_keys(min(starts, default=first), max(ends, default=last))
    labels = [f"T{m:%m/%Y}" for m in months]
    actual_columns = [f"TỔNG TÍCH LŨY {label}" for label in labels]
    slot_columns = [f"SỐ SUẤT ĐẠT {label}" for label in labels]
    columns = BASE_COLUMNS + actual_columns + slot_columns + ["TỔNG SỐ SUẤT ĐẠT"] + EXTRA_COLUMNS
    # One shared read and sales normalization per historical month, not per programme.
    loaded: dict[date, pd.DataFrame | None] = {}

    def once(month: date) -> pd.DataFrame | None:
        if month not in loaded:
            loaded[month] = load_month(month)
        return loaded[month]

    buckets: dict[date, Any] = {}
    for month in months:
        if month > last:
            continue
        if once(month) is None:
            buckets[month] = None
            continue
        # Deliveries of the month, also from orders created in a neighbour month.
        lines, _, _ = calc.delivered_lines(
            once, month, min(date(month.year, month.month, monthrange(month.year, month.month)[1]), last),
            cfg, newest or last)
        by_customer = defaultdict(list)
        for line in lines:
            by_customer[line["customer"]].append(line)
        buckets[month] = by_customer
    records = []
    for result in results:
        try:
            rule = calc.parse_rule(result.program)
        except ValueError:
            continue  # unsupported catalogue entries remain explicit in ChuongTrinh
        start, end = program_day(result.program, "startDate") or first, program_day(result.program, "endDate") or last
        mode = modes.get(result.program_id, "")
        # compute sorts the same deduplicated registered IDs used here.
        rows = dict(zip(sorted(rule.customers), result.rows, strict=True))
        for customer, row in rows.items():
            values, slots, source_notes, assignments = [], [], set(), defaultdict(set)
            running, previous_slots, complete = 0.0, 0, True
            for month in months:
                if month < start.replace(day=1) or month > end or month > last:
                    values.append(None)
                    slots.append(None)
                    continue
                bucket = buckets.get(month)
                if bucket is None:
                    values.append(None)
                    slots.append(None)
                    source_notes.add(f"Thiếu đơn hàng {month:%m/%Y}")
                    complete = False
                    continue
                matching = [line for line in bucket.get(customer, []) if line["sku"] in rule.units
                            and (mode != "cumulative" or start <= calc.local_day(line["raw"].get("ngay_giao_hang")) <= end)]
                # Do not understate historical progress when a qualifying sale lacks units.
                if any(not line["unit"] for line in matching):
                    values.append(None)
                    slots.append(None)
                    source_notes.add(f"Thiếu ĐVT dòng bán {month:%m/%Y}")
                    complete = False
                    continue
                counted = [line for line in matching if line["unit"] in rule.units[line["sku"]]]
                # Keep source precision through threshold checks, as compute() does.
                # Rounding 71.999 to 72 before checking a 72-unit minimum grants a false slot.
                actual = sum(line["amount"] if rule.kind == calc.AMOUNT else line["quantity"] for line in counted)
                values.append(actual)
                running += actual
                if mode == "monthly":
                    slots.append(calc.reward_multiplier(actual, rule))
                elif mode == "cumulative" and complete:
                    achieved = calc.reward_multiplier(running, rule)
                    slots.append(achieved - previous_slots)
                    previous_slots = achieved
                else:
                    slots.append(None)
                for line in counted:
                    raw = line.get("raw") or {}
                    meta = resolve_sales(raw, cfg)
                    meta.update({"Tên NVBH": text(raw.get("ten_nguoi_dat")), "Route": text(raw.get("tuyen_code"))})
                    for field, value in meta.items():
                        if value:
                            assignments[field].add(value)
            meta = customers.get(customer, {})
            current = (sum(row["_weights"]) if "_weights" in row else
                       (row.get("objThucHien") or {}).get(calc.TARGET_ID))
            total = running if complete else None
            if rule.tiers:
                selected = next((tier for tier in rule.tiers if current is not None and calc.reached(current, tier)), rule.tiers[0])
            else:
                selected = rule
            target = selected.minimum
            if not mode:
                source_notes.add("Chưa xác định cách tính")
            record = {
                "#": len(records) + 1, "Vùng": row.get("kv") or _assigned(assignments, source_notes, "Vùng"),
                "NPP": row.get("npp") or _assigned(assignments, source_notes, "Tên NPP"),
                "Tên SS": _assigned(assignments, source_notes, "SS Name"),
                "Tên NVBH": _assigned(assignments, source_notes, "Tên NVBH"),
                "Route": _assigned(assignments, source_notes, "Route") or meta.get("Route"),
                "Mã KH": row.get("ma"), "Tên Điểm bán": row.get("ten"),
                "Tên người liên hệ": meta.get("Tên người liên hệ"), "Loại KH": row.get("loai"),
                "Địa chỉ": row.get("dc"), "Số điện thoại": row.get("sdt"), "Tên CT": result.program_name,
                "NGÀY ĐĂNG KÝ": meta.get("Ngày đăng ký CT", {}).get(result.program_id),
                "MỨC": calc.bonus_code(result.program_name).split(" - ")[-1],
                "MỤC TIÊU": target, "SỐ SUẤT": 1, "MỤC TIÊU HOÀN THÀNH": target,
                "TỔNG TÍCH LŨY": total,
                "CÒN LẠI": min(0, current - target) if current is not None and mode else None,
                "TỔNG SỐ SUẤT ĐẠT": sum(s for s in slots if s is not None) if complete and mode else None,
                "Mã CT": calc.bonus_code(result.program_name),
                "ĐVT mục tiêu": "đồng" if rule.kind == calc.AMOUNT else "; ".join(sorted({u for units in rule.units.values() for u in units})),
                "Cách tính": {"monthly": "Theo tháng", "cumulative": "Cả kỳ"}.get(mode, "Chưa xác định"),
                "Thực hiện kỳ xét thưởng": current, "Trạng thái trả thưởng": row.get("_eligible"),
            }
            record["Thông tin nguồn"] = "; ".join(sorted(source_notes))
            record.update(zip(actual_columns, values, strict=True))
            record.update(zip(slot_columns, slots, strict=True))
            records.append(record)
    frame = pd.DataFrame(records, columns=columns, dtype=object)
    frame.attrs["months"] = [m.isoformat() for m in months]
    frame.attrs["missing_months"] = [f"{m:%Y-%m}" for m, bucket in buckets.items() if bucket is None]
    return frame
