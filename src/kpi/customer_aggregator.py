"""Aggregate order/visit data into one row per Sales employee + customer.

The output contains only business facts. Image-label dependent fields are kept
as Excel formulas by :mod:`kpi.exporter`, so a manual image-label change
recalculates KPI immediately without rerunning Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .kpi_rules import (
    DEFAULT_KPI_POLICY,
    KPIPolicy,
    ascii_key,
    extract_order_id,
    is_truthy,
    is_valid_sign_note,
)


REQUIRED_VISIT_COLUMNS = {
    "ten_nhan_vien",
    "ngay",
    "ma_kh",
    "ten_kh",
    "ghi_ton",
    "ghi_chu",
}
REQUIRED_ORDER_COLUMNS = {"ma_kh", "ngay_dat", "ma_dvt", "so_luong"}
VN_TIMEZONE = "Asia/Ho_Chi_Minh"


@dataclass(frozen=True)
class KPIAggregationResult:
    customers: pd.DataFrame
    period_start: pd.Timestamp
    previous_period_start: pd.Timestamp
    history_start: pd.Timestamp | None
    warnings: tuple[str, ...]


def _require_columns(frame: pd.DataFrame, required: set[str], sheet_name: str) -> None:
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            f"Sheet {sheet_name} thiếu cột bắt buộc: {', '.join(sorted(missing))}"
        )


def _month_start(now: datetime | pd.Timestamp) -> pd.Timestamp:
    stamp = pd.Timestamp(now)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(VN_TIMEZONE).tz_localize(None)
    return pd.Timestamp(year=stamp.year, month=stamp.month, day=1)


def _business_datetime(value: object) -> pd.Timestamp | pd.NaT:
    """Return a timezone-naive Vietnam business timestamp.

    Persisted VisitData may contain raw UTC ISO timestamps while the monthly
    master also contains ``_sync_date`` as the canonical local calendar date.
    Naive values are preserved as already-local business timestamps; aware
    values are converted to Asia/Ho_Chi_Minh before removing timezone metadata.
    """

    if value is None:
        return pd.NaT
    try:
        if bool(pd.isna(value)):
            return pd.NaT
    except (TypeError, ValueError):
        pass
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return pd.NaT
    if stamp.tzinfo is not None:
        return stamp.tz_convert(VN_TIMEZONE).tz_localize(None)
    return stamp


def _business_dates(values: pd.Series) -> pd.Series:
    return values.map(_business_datetime)


def _nonblank_last(values: Iterable[object]) -> str:
    result = ""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.casefold() != "nan":
            result = text
    return result


def _join_notes(values: Iterable[object]) -> str:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value is None:
            continue
        text = " ".join(str(value).strip().split())
        if not text or text.casefold() == "nan":
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return " | ".join(output)


def _prepare_visits(frame: pd.DataFrame) -> pd.DataFrame:
    _require_columns(frame, REQUIRED_VISIT_COLUMNS, "Data_anh")
    visits = frame.copy()
    # `_sync_date` is generated from the local report partition and is the most
    # stable business date for flat VisitData. Fall back to raw `ngay` for
    # legacy/local input workbooks that do not carry the partition column.
    date_source = visits["_sync_date"] if "_sync_date" in visits.columns else visits["ngay"]
    visits["_date"] = _business_dates(date_source)
    visits["_customer_key"] = visits["ma_kh"].map(ascii_key)
    visits["_employee_key"] = visits["ten_nhan_vien"].map(ascii_key)
    visits = visits[
        visits["_date"].notna()
        & visits["_customer_key"].ne("")
        & visits["_employee_key"].ne("")
    ].copy()
    visits["_ghi_ton"] = visits["ghi_ton"].map(is_truthy)
    visits["_valid_sign_note"] = visits["ghi_chu"].map(is_valid_sign_note)
    return visits


def _prepare_orders(frame: pd.DataFrame, policy: KPIPolicy) -> pd.DataFrame:
    _require_columns(frame, REQUIRED_ORDER_COLUMNS, "Data_don_hang")
    orders = frame.copy()
    if "is_km" in orders.columns:
        orders = orders.loc[~orders["is_km"].map(is_truthy)].copy()
    orders["_date"] = _business_dates(orders["ngay_dat"])
    orders["_customer_key"] = orders["ma_kh"].map(ascii_key)
    employee_series = (
        orders["ten_nguoi_dat"]
        if "ten_nguoi_dat" in orders.columns
        else pd.Series("", index=orders.index)
    )
    orders["_employee_key"] = employee_series.map(ascii_key)
    orders["_unit_key"] = orders["ma_dvt"].map(ascii_key)
    orders["_qty"] = pd.to_numeric(orders["so_luong"], errors="coerce").fillna(0.0)
    allowed_units = set(policy.ktb_units)
    orders["_ktb_qty"] = np.where(
        orders["_unit_key"].isin(allowed_units), orders["_qty"], 0.0
    )
    direct_order = (
        orders["ma_phieu"].fillna("").astype(str).str.strip()
        if "ma_phieu" in orders.columns
        else pd.Series("", index=orders.index)
    )
    description = (
        orders["dien_giai"]
        if "dien_giai" in orders.columns
        else pd.Series("", index=orders.index)
    )
    orders["_order_id"] = direct_order.where(
        direct_order.ne(""), description.map(extract_order_id)
    )
    missing_order_id = orders["_order_id"].eq("")
    # Fallback is intentionally deterministic and conservative. Exact order
    # timestamps normally group product lines of the same order together.
    orders.loc[missing_order_id, "_order_id"] = (
        orders.loc[missing_order_id, "_customer_key"]
        + "|"
        + orders.loc[missing_order_id, "_date"].astype(str)
        + "|"
        + orders.loc[missing_order_id, "_employee_key"]
    )
    orders = orders[
        orders["_date"].notna()
        & orders["_customer_key"].ne("")
    ].copy()
    return orders


def aggregate_customer_kpi(
    visits_raw: pd.DataFrame,
    orders_raw: pd.DataFrame,
    now: datetime | pd.Timestamp,
    policy: KPIPolicy = DEFAULT_KPI_POLICY,
) -> KPIAggregationResult:
    """Build one KPI fact row for every customer visited in month M."""

    visits = _prepare_visits(visits_raw)
    orders = _prepare_orders(orders_raw, policy)
    current_start = _month_start(now)
    next_start = current_start + pd.offsets.MonthBegin(1)
    previous_start = current_start - pd.offsets.MonthBegin(1)

    current_visits = visits[
        (visits["_date"] >= current_start) & (visits["_date"] < next_start)
    ].copy()
    recent_visits = visits[
        (visits["_date"] >= previous_start) & (visits["_date"] < next_start)
    ].copy()
    recent_orders = orders[
        (orders["_date"] >= previous_start) & (orders["_date"] < next_start)
    ].copy()

    warnings: list[str] = []
    history_parts = [
        series
        for series in (visits["_date"].dropna(), orders["_date"].dropna())
        if not series.empty
    ]
    history_dates = (
        pd.concat(history_parts, ignore_index=True)
        if history_parts
        else pd.Series([], dtype="datetime64[ns]")
    )
    history_start = history_dates.min() if not history_dates.empty else None
    if history_start is not None and history_start >= current_start - pd.DateOffset(months=6):
        warnings.append(
            "Nguồn lịch sử bắt đầu từ "
            f"{history_start.date().isoformat()}; phân loại Mới/Cũ chỉ chính xác "
            "trong phạm vi lịch sử nguồn hiện có."
        )

    columns = [
        "ten_nhan_vien",
        "ma_kh",
        "ten_kh",
        "visit_count_m",
        "first_activity_date",
        "max_order_2m_ktb",
        "total_order_2m_ktb",
        "order_count_2m",
        "ghi_ton_2m",
        "valid_sign_note_2m",
        "ghi_chu_2m",
        "period_start",
    ]
    if current_visits.empty:
        return KPIAggregationResult(
            customers=pd.DataFrame(columns=columns),
            period_start=current_start,
            previous_period_start=previous_start,
            history_start=history_start,
            warnings=tuple(warnings),
        )

    owner_counts = current_visits.groupby("_customer_key")["_employee_key"].nunique()
    duplicate_owners = int((owner_counts > 1).sum())
    if duplicate_owners:
        warnings.append(
            f"Có {duplicate_owners} khách hàng xuất hiện dưới nhiều nhân viên trong tháng M; "
            "KPI sẽ hiển thị từng dòng theo nhân viên viếng thăm nhưng bằng chứng 2 tháng được ghép theo Mã KH."
        )

    visit_first = visits.groupby("_customer_key")["_date"].min()
    order_first = orders.groupby("_customer_key")["_date"].min()
    activity_index = visit_first.index.union(order_first.index)
    first_activity = pd.DataFrame(index=activity_index)
    first_activity["visit_first"] = pd.to_datetime(
        visit_first.reindex(activity_index), errors="coerce"
    )
    first_activity["order_first"] = pd.to_datetime(
        order_first.reindex(activity_index), errors="coerce"
    )
    first_activity["first_activity_date"] = first_activity[
        ["visit_first", "order_first"]
    ].min(axis=1)

    recent_visit_agg = (
        recent_visits.groupby(["_customer_key"], as_index=False)
        .agg(
            ghi_ton_2m=("_ghi_ton", "max"),
            valid_sign_note_2m=("_valid_sign_note", "max"),
            ghi_chu_2m=("ghi_chu", _join_notes),
        )
    )

    order_totals = (
        recent_orders.groupby(["_customer_key", "_order_id"], as_index=False)["_ktb_qty"]
        .sum()
        .rename(columns={"_ktb_qty": "order_total_ktb"})
    )
    if order_totals.empty:
        order_agg = pd.DataFrame(
            columns=[
                "_customer_key",
                "max_order_2m_ktb",
                "total_order_2m_ktb",
                "order_count_2m",
            ]
        )
    else:
        order_agg = (
            order_totals.groupby(["_customer_key"], as_index=False)
            .agg(
                max_order_2m_ktb=("order_total_ktb", "max"),
                total_order_2m_ktb=("order_total_ktb", "sum"),
                order_count_2m=("_order_id", "nunique"),
            )
        )

    current = (
        current_visits.groupby(["_employee_key", "_customer_key"], as_index=False)
        .agg(
            ten_nhan_vien=("ten_nhan_vien", _nonblank_last),
            ma_kh=("ma_kh", _nonblank_last),
            ten_kh=("ten_kh", _nonblank_last),
            # Every row in the normalized VisitData master is one visit event.
            # Counting dates would undercount repeated visits on the same day.
            visit_count_m=("_date", "size"),
        )
    )
    current = current.merge(
        recent_visit_agg, on=["_customer_key"], how="left"
    ).merge(order_agg, on=["_customer_key"], how="left")
    current["first_activity_date"] = current["_customer_key"].map(
        first_activity["first_activity_date"]
    )
    current["period_start"] = current_start

    for name in ("max_order_2m_ktb", "total_order_2m_ktb", "order_count_2m"):
        current[name] = pd.to_numeric(current[name], errors="coerce").fillna(0)
    current["ghi_ton_2m"] = current["ghi_ton_2m"].fillna(False).astype(bool)
    current["valid_sign_note_2m"] = (
        current["valid_sign_note_2m"].fillna(False).astype(bool)
    )
    current["ghi_chu_2m"] = current["ghi_chu_2m"].fillna("")

    result = current[columns].sort_values(
        ["ten_nhan_vien", "ma_kh"], kind="stable"
    ).reset_index(drop=True)
    return KPIAggregationResult(
        customers=result,
        period_start=current_start,
        previous_period_start=previous_start,
        history_start=history_start,
        warnings=tuple(warnings),
    )


def load_and_aggregate_customer_kpi(
    workbook_path: str | Path,
    now: datetime | pd.Timestamp,
    policy: KPIPolicy = DEFAULT_KPI_POLICY,
) -> KPIAggregationResult:
    path = Path(workbook_path)
    visits = pd.read_excel(path, sheet_name="Data_anh")
    orders = pd.read_excel(path, sheet_name="Data_don_hang")
    return aggregate_customer_kpi(visits, orders, now=now, policy=policy)
