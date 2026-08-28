from __future__ import annotations

import calendar
import math
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

from ..normalize import clean_text, normalize_code, to_number
from .common import Row, date_value, roundup_excel


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
            rounded = roundup_excel(need / multiple) * multiple if multiple else 0.0
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
        and date_value(row.get("Ngay bat dau SX")) is not None
    ]
    eligible.sort(
        key=lambda row: (
            clean_text(row.get("Chuyen")),
            date_value(row.get("Ngay bat dau SX")) or date.max,
            int(to_number(row.get("Source row"))),
        )
    )
    previous_by_line: dict[str, tuple[Any, float]] = {}
    result: dict[int, tuple[float, float]] = {}
    for row in eligible:
        line = clean_text(row.get("Chuyen"))
        per_shift = to_number(row.get("So luong/ca"))
        shifts_day = to_number(row.get("So ca/ngay"))
        start_date = date_value(row.get("Ngay bat dau SX"))
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
        start = date_value(row.get("Ngay bat dau SX"))
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

        scheduled = sum(day_quantities.values())
        remaining = max(q - scheduled, 0.0)
        if remaining < 1e-6:
            remaining = 0.0

        output.append(
            {
                "Ma SP": code,
                "Ten SP": row.get("Ten SP"),
                "DVT": row.get("DVT"),
                "Chuyen": line,
                "SL ke hoach": q,
                "Ngay bat dau SX": start,
                "SL da xep": scheduled,
                "SL chua xep": remaining,
                **{d.isoformat(): qty for d, qty in day_quantities.items()},
            }
        )
    return output
