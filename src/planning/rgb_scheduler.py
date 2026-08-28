from __future__ import annotations

import calendar
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from .normalize import clean_text, normalize_code, to_number

Row = Mapping[str, Any]


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def build_rgb_daily_schedule(
    weekly_rows: Iterable[Row],
    *,
    plan_year: int,
    plan_month: int,
) -> list[dict[str, Any]]:
    """Build a deterministic schedule for the single shared RGB line.

    The workbook's RGB daily cells are manually curated, so there is no formula
    chain to port. This scheduler uses only explicit weekly-plan constraints:
    earliest start date, quantity per shift and shifts per day. RGB products run
    sequentially on one shared line and are ordered by earliest start then source
    row. The last shift may be partial so scheduled quantity exactly matches the
    weekly rounded production quantity when the month has enough capacity.
    """
    rows = [
        row
        for row in weekly_rows
        if clean_text(row.get("Chuyen")) == "RGB"
        and to_number(row.get("SL SX tron me/ca")) > 0
        and _date_value(row.get("Ngay bat dau SX")) is not None
    ]
    rows.sort(
        key=lambda row: (
            _date_value(row.get("Ngay bat dau SX")) or date.max,
            int(to_number(row.get("Source row"))),
        )
    )
    if not rows:
        return []

    first = date(plan_year, plan_month, 1)
    days_in_month = calendar.monthrange(plan_year, plan_month)[1]
    shifts_per_day = max(
        1.0,
        max(to_number(row.get("So ca/ngay")) for row in rows),
    )
    month_end_shift = days_in_month * shifts_per_day
    cursor = 0.0
    output: list[dict[str, Any]] = []

    for row in rows:
        start = _date_value(row.get("Ngay bat dau SX"))
        if start is None:
            continue
        per_shift = to_number(row.get("So luong/ca"))
        planned = to_number(row.get("SL SX tron me/ca"))
        if per_shift <= 0 or planned <= 0:
            continue

        earliest = max(0.0, (start - first).days * shifts_per_day)
        start_shift = max(cursor, earliest)
        required_shifts = planned / per_shift
        end_shift = start_shift + required_shifts
        schedulable_end = min(end_shift, month_end_shift)

        day_quantities: dict[str, float] = {}
        for day in range(1, days_in_month + 1):
            day_start = (day - 1) * shifts_per_day
            day_end = day * shifts_per_day
            overlap = max(
                0.0,
                min(schedulable_end, day_end) - max(start_shift, day_start),
            )
            qty = overlap * per_shift
            if qty > 0:
                day_quantities[date(plan_year, plan_month, day).isoformat()] = qty

        scheduled = sum(day_quantities.values())
        remaining = max(0.0, planned - scheduled)
        auto_start_day = int(start_shift // shifts_per_day)
        output.append(
            {
                "Ma SP": normalize_code(row.get("Ma SP")),
                "Ten SP": row.get("Ten SP"),
                "DVT": row.get("DVT"),
                "Chuyen": "RGB",
                "Nhom SP": clean_text(row.get("Nhom SP")),
                "SL ke hoach": planned,
                "Ngay bat dau SX": start,
                "Ngay bat dau auto": date.fromordinal(
                    first.toordinal() + auto_start_day
                ),
                "SL chua xep": remaining,
                **day_quantities,
            }
        )
        cursor = end_shift

    return output
