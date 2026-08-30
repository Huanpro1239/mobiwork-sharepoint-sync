"""Compact persistent customer-history master for KPI New/Old classification.

The production KPI only needs the earliest known order/visit per customer to
classify New vs Old.  Re-reading many years of monthly workbooks on every run is
wasteful, so this module keeps one compact row per customer on SharePoint.

Bootstrap mode scans historical monthly masters one file at a time and retains
only min/max dates.  Incremental mode then updates the master from the rolling
M-1/M KPI inputs, preserving the earliest dates forever.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import os
from pathlib import Path
from typing import Any

import pandas as pd

from kpi.customer_aggregator import KPIAggregationResult, _business_dates
from kpi.kpi_rules import ascii_key, is_truthy
from sharepoint_kpi_source import KPIInputBundle


HISTORY_SCHEMA_VERSION = "1.0"
DEFAULT_REMOTE_PATH = "KPI/History/customer_history.csv"
HISTORY_COLUMNS = (
    "ma_kh",
    "ten_kh",
    "first_visit_date",
    "first_order_date",
    "first_activity_date",
    "last_visit_date",
    "last_order_date",
    "last_activity_date",
    "ever_visit",
    "ever_order",
    "schema_version",
    "updated_at_utc",
)


@dataclass(frozen=True)
class CustomerHistoryStatus:
    history: pd.DataFrame
    remote_path: str
    initialized_now: bool
    bootstrap_source_files: int
    incremental_source_files: int
    warnings: tuple[str, ...]


def remote_history_path() -> str:
    configured = os.environ.get("CUSTOMER_HISTORY_REMOTE_PATH", "").strip().strip("/")
    return configured or DEFAULT_REMOTE_PATH


def empty_history() -> pd.DataFrame:
    return pd.DataFrame(columns=HISTORY_COLUMNS)


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    return " ".join(str(value).strip().split())


def _last_nonblank(values: pd.Series) -> str:
    result = ""
    for value in values:
        text = _clean_text(value)
        if text:
            result = text
    return result


def _prepare_existing(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return empty_history()
    history = frame.copy()
    required = {"ma_kh", "first_activity_date"}
    missing = required - set(history.columns)
    if missing:
        raise ValueError(
            "Customer history thiếu cột bắt buộc: " + ", ".join(sorted(missing))
        )
    history["_customer_key"] = history["ma_kh"].map(ascii_key)
    if history["_customer_key"].eq("").any():
        raise ValueError("Customer history có Mã KH rỗng/không hợp lệ")
    if history["_customer_key"].duplicated().any():
        duplicates = history.loc[history["_customer_key"].duplicated(False), "ma_kh"].tolist()
        raise ValueError(f"Customer history có Mã KH trùng: {duplicates[:10]}")
    for column in (
        "first_visit_date",
        "first_order_date",
        "first_activity_date",
        "last_visit_date",
        "last_order_date",
        "last_activity_date",
    ):
        if column not in history.columns:
            history[column] = pd.NaT
        history[column] = pd.to_datetime(history[column], errors="coerce")
    for column in ("ever_visit", "ever_order"):
        if column not in history.columns:
            history[column] = False
        history[column] = history[column].map(is_truthy).astype(bool)
    if "ten_kh" not in history.columns:
        history["ten_kh"] = ""
    return history


def _event_facts(
    frame: pd.DataFrame,
    *,
    event: str,
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    if "ma_kh" not in frame.columns:
        raise ValueError(f"Nguồn {event} thiếu cột ma_kh")
    if event == "visit":
        source_dates = frame["_sync_date"] if "_sync_date" in frame.columns else frame.get("ngay")
        if source_dates is None:
            raise ValueError("Nguồn visit thiếu _sync_date/ngay")
    elif event == "order":
        if "ngay_dat" not in frame.columns:
            raise ValueError("Nguồn order thiếu ngay_dat")
        source_dates = frame["ngay_dat"]
    else:
        raise ValueError(f"Unsupported event type: {event}")

    facts = pd.DataFrame(index=frame.index)
    facts["_customer_key"] = frame["ma_kh"].map(ascii_key)
    facts["ma_kh"] = frame["ma_kh"].map(_clean_text)
    facts["ten_kh"] = frame["ten_kh"].map(_clean_text) if "ten_kh" in frame.columns else ""
    facts["_date"] = _business_dates(source_dates)
    facts = facts[facts["_customer_key"].ne("") & facts["_date"].notna()].copy()
    if facts.empty:
        return facts
    return (
        facts.groupby("_customer_key", as_index=False)
        .agg(
            ma_kh=("ma_kh", _last_nonblank),
            ten_kh=("ten_kh", _last_nonblank),
            first_date=("_date", "min"),
            last_date=("_date", "max"),
        )
    )


def _min_date(left: object, right: object) -> pd.Timestamp | pd.NaT:
    values = pd.to_datetime(pd.Series([left, right]), errors="coerce").dropna()
    return values.min() if not values.empty else pd.NaT


def _max_date(left: object, right: object) -> pd.Timestamp | pd.NaT:
    values = pd.to_datetime(pd.Series([left, right]), errors="coerce").dropna()
    return values.max() if not values.empty else pd.NaT


def update_customer_history(
    history_raw: pd.DataFrame,
    visits: pd.DataFrame,
    orders: pd.DataFrame,
    *,
    updated_at: datetime | None = None,
) -> pd.DataFrame:
    """Merge new facts without ever moving a first-activity date forward."""

    history = _prepare_existing(history_raw)
    state: dict[str, dict[str, Any]] = {}
    for record in history.to_dict(orient="records"):
        key = str(record.pop("_customer_key"))
        state[key] = record

    now_utc = (updated_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    for event, frame in (("visit", visits), ("order", orders)):
        facts = _event_facts(frame, event=event)
        for fact in facts.to_dict(orient="records"):
            key = str(fact["_customer_key"])
            current = state.get(
                key,
                {
                    "ma_kh": fact["ma_kh"],
                    "ten_kh": fact["ten_kh"],
                    "first_visit_date": pd.NaT,
                    "first_order_date": pd.NaT,
                    "first_activity_date": pd.NaT,
                    "last_visit_date": pd.NaT,
                    "last_order_date": pd.NaT,
                    "last_activity_date": pd.NaT,
                    "ever_visit": False,
                    "ever_order": False,
                },
            )
            if fact["ma_kh"]:
                current["ma_kh"] = fact["ma_kh"]
            if fact["ten_kh"]:
                current["ten_kh"] = fact["ten_kh"]
            first_column = f"first_{event}_date"
            last_column = f"last_{event}_date"
            current[first_column] = _min_date(current.get(first_column), fact["first_date"])
            current[last_column] = _max_date(current.get(last_column), fact["last_date"])
            current[f"ever_{event}"] = True
            current["first_activity_date"] = _min_date(
                current.get("first_visit_date"), current.get("first_order_date")
            )
            current["last_activity_date"] = _max_date(
                current.get("last_visit_date"), current.get("last_order_date")
            )
            current["schema_version"] = HISTORY_SCHEMA_VERSION
            current["updated_at_utc"] = now_utc
            state[key] = current

    if not state:
        return empty_history()
    result = pd.DataFrame(state.values())
    for column in HISTORY_COLUMNS:
        if column not in result.columns:
            result[column] = ""
    result = result[list(HISTORY_COLUMNS)].sort_values("ma_kh", kind="stable").reset_index(drop=True)
    return result


def load_customer_history(client: Any, drive_id: str, remote_path: str | None = None) -> pd.DataFrame:
    path = remote_path or remote_history_path()
    content = client.download_file_bytes(drive_id, path)
    if not content:
        return empty_history()
    frame = pd.read_csv(BytesIO(content), dtype=str, keep_default_na=False)
    return _prepare_existing(frame).drop(columns=["_customer_key"])


def history_csv_bytes(history: pd.DataFrame) -> bytes:
    output = history.copy()
    for column in (
        "first_visit_date",
        "first_order_date",
        "first_activity_date",
        "last_visit_date",
        "last_order_date",
        "last_activity_date",
    ):
        if column in output.columns:
            values = pd.to_datetime(output[column], errors="coerce")
            output[column] = values.dt.strftime("%Y-%m-%d").fillna("")
    return output.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")


def _rolling_paths(source: Any, report_key: str, now: datetime | pd.Timestamp) -> list[str]:
    current = pd.Timestamp(now)
    if current.tzinfo is not None:
        current = current.tz_convert("Asia/Ho_Chi_Minh").tz_localize(None)
    current = current.replace(day=1).normalize()
    previous = current - pd.offsets.MonthBegin(1)
    all_paths = source._discover_report_workbooks(report_key, current)
    tokens = {f"/{previous:%Y}/{previous:%m}/", f"/{current:%Y}/{current:%m}/"}
    return [path for path in all_paths if any(token in f"/{path}" for token in tokens)]


def load_rolling_kpi_inputs(source: Any, now: datetime | pd.Timestamp) -> KPIInputBundle:
    """Load only M-1/M workbook contents; older history comes from the compact master."""

    visit_paths = _rolling_paths(source, "visit", now)
    order_paths = _rolling_paths(source, "order", now)
    if not visit_paths:
        raise FileNotFoundError("Không tìm thấy monthly master viếng thăm M-1/M trên SharePoint")

    visits = [source._read_excel(path, "Data") for path in visit_paths]
    orders: list[pd.DataFrame] = []
    promo_rows = 0
    for path in order_paths:
        frame = source._read_excel(path, "ChiTietSP")
        if "is_km" in frame.columns:
            promo = frame["is_km"].map(is_truthy)
            promo_rows += int(promo.sum())
            frame = frame.loc[~promo].copy()
        orders.append(frame)

    visit_frame = pd.concat(visits, ignore_index=True, sort=False) if visits else pd.DataFrame()
    order_frame = pd.concat(orders, ignore_index=True, sort=False) if orders else pd.DataFrame()
    defaults = {
        "ten_nhan_vien": "",
        "ma_kh": "",
        "ten_kh": "",
        "ngay": pd.NaT,
        "ghi_ton": False,
        "ghi_chu": "",
        "hinh_anh": "",
        "stt_hinh": "",
    }
    for column, default in defaults.items():
        if column not in visit_frame.columns:
            visit_frame[column] = default
    order_defaults = {
        "ma_kh": "",
        "ten_kh": "",
        "ngay_dat": pd.NaT,
        "ten_nguoi_dat": "",
        "ma_dvt": "",
        "so_luong": 0,
        "ma_phieu": "",
        "dien_giai": "",
    }
    for column, default in order_defaults.items():
        if column not in order_frame.columns:
            order_frame[column] = default

    warnings: list[str] = []
    if promo_rows:
        warnings.append(f"Đã loại {promo_rows:,} dòng sản phẩm khuyến mãi khỏi sản lượng KPI.")
    if not order_paths:
        warnings.append("Chưa có monthly master đơn đặt hàng M-1/M; điều kiện doanh số sẽ không đạt.")
    return KPIInputBundle(
        visits=visit_frame,
        orders=order_frame,
        visit_sources=tuple(visit_paths),
        order_sources=tuple(order_paths),
        warnings=tuple(warnings),
    )


def bootstrap_customer_history(source: Any, now: datetime | pd.Timestamp) -> tuple[pd.DataFrame, int]:
    """One-time memory-bounded bootstrap over all discovered historical masters."""

    through = pd.Timestamp(now)
    history = empty_history()
    processed = 0
    for path in source._discover_report_workbooks("visit", through):
        history = update_customer_history(history, source._read_excel(path, "Data"), pd.DataFrame())
        processed += 1
    for path in source._discover_report_workbooks("order", through):
        frame = source._read_excel(path, "ChiTietSP")
        if "is_km" in frame.columns:
            frame = frame.loc[~frame["is_km"].map(is_truthy)].copy()
        history = update_customer_history(history, pd.DataFrame(), frame)
        processed += 1
    return history, processed


def apply_history_to_kpi(
    result: KPIAggregationResult,
    history: pd.DataFrame,
) -> KPIAggregationResult:
    """Replace recent-window first activity with the compact all-history truth."""

    customers = result.customers.copy()
    prepared = _prepare_existing(history)
    first_map = prepared.set_index("_customer_key")["first_activity_date"] if not prepared.empty else pd.Series(dtype="datetime64[ns]")
    customer_keys = customers["ma_kh"].map(ascii_key) if not customers.empty else pd.Series(dtype=str)
    if not customers.empty:
        customers["first_activity_date"] = customer_keys.map(first_map)
    missing = int(customers["first_activity_date"].isna().sum()) if not customers.empty else 0
    warnings = tuple(
        warning
        for warning in result.warnings
        if not warning.startswith("Nguồn lịch sử bắt đầu từ ")
    )
    extra: list[str] = []
    if missing:
        extra.append(
            f"Customer history thiếu {missing} KH đang xét; các KH này sẽ hiển thị loại 'Không rõ'."
        )
    history_start = None
    if not prepared.empty:
        valid = pd.to_datetime(prepared["first_activity_date"], errors="coerce").dropna()
        history_start = valid.min() if not valid.empty else None
    return KPIAggregationResult(
        customers=customers,
        period_start=result.period_start,
        previous_period_start=result.previous_period_start,
        history_start=history_start,
        warnings=warnings + tuple(extra),
    )


def build_customer_history_status(
    source: Any,
    client: Any,
    drive_id: str,
    now: datetime | pd.Timestamp,
) -> tuple[CustomerHistoryStatus, KPIInputBundle]:
    """Load/initialize history, then update it from only the rolling KPI window."""

    remote = remote_history_path()
    existing = load_customer_history(client, drive_id, remote)
    initialized_now = existing.empty
    bootstrap_files = 0
    warnings: list[str] = []
    if initialized_now:
        existing, bootstrap_files = bootstrap_customer_history(source, now)
        warnings.append(
            f"Customer history được bootstrap lần đầu từ {bootstrap_files} monthly master; các lần sau chỉ đọc M-1/M."
        )

    inputs = load_rolling_kpi_inputs(source, now)
    updated = update_customer_history(existing, inputs.visits, inputs.orders)
    local_path = Path(os.environ.get("KPI_OUTPUT_DIR", "runtime/output")) / "customer_history.csv"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(history_csv_bytes(updated))
    status = CustomerHistoryStatus(
        history=updated,
        remote_path=remote,
        initialized_now=initialized_now,
        bootstrap_source_files=bootstrap_files,
        incremental_source_files=len(inputs.visit_sources) + len(inputs.order_sources),
        warnings=tuple(warnings),
    )
    return status, inputs
