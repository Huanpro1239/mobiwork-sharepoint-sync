from __future__ import annotations

import calendar
import math
from datetime import date, datetime
from typing import Any, Mapping

Row = Mapping[str, Any]


def date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def add_months(value: date, months: int) -> date:
    idx = value.year * 12 + value.month - 1 + months
    year, month0 = divmod(idx, 12)
    return date(year, month0 + 1, 1)


def month_end(value: date) -> date:
    return date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])


def roundup_excel(value: float) -> int:
    """Match Excel ROUNDUP semantics for positive and negative values."""
    return math.ceil(value) if value >= 0 else -math.ceil(abs(value))


def ceil_to_moq(value: float, moq: float) -> float:
    if value <= 0:
        return 0.0
    if moq <= 0:
        return value
    return math.ceil(value / moq) * moq
