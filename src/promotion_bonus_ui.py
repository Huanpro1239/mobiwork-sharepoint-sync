"""Promotion Bonus report through the DMS web (Paybonus page) service.

Evidence (docs/PROMOTION_BONUS_EVIDENCE.md, 2026-10-06):
- The OpenAPI PromotionBonusReport returns zero customer rows for every probed
  date binding, so it cannot produce the report.
- The Paybonus page calls POST {service}/PromotionBonusReport with query
  orgid/projectID/projectName/assignTo/eeName/idcustomer/[sttt]/startDate/endDate
  (epoch ms at local midnight) and body {"arrCT": [...]}. Called with exactly
  ONE program it returns final customer rows (ma, ten, kv, npp, objThucHien...).
- The same request sometimes returns nested ``_Orders`` envelopes instead of
  final rows. That is a transient server state: retry, never read it as
  "no customers".
- The web service authenticates with Basic email:tokenkey plus ``x-alias``.

This module only reads DMS. Target/remaining/rate maths mirrors
``renderData`` in formPaybonusReport.js so the workbook matches the page.
"""
from __future__ import annotations

import base64
import calendar
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
import requests

LOG = logging.getLogger("mobiwork_promotion_bonus_ui")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
DEFAULT_SERVICE_URL = "https://api.mobiwork.vn:3019"
OBJECT_ID = re.compile(r"[a-fA-F0-9]{24}")
STTT_VALUES = {"", "0", "1", "-1", "2"}
STTT_LABELS = {
    "": "Mặc định (không gửi sttt)",
    "0": "Tổng tiền (Đơn giá * Số lượng)",
    "1": "Tổng tiền - chiết khấu SP",
    "-1": "Tổng tiền - chiết khấu SP - CK đơn hàng",
    "2": "Tổng tiền có VAT (Đơn giá * Số lượng + VAT)",
}

FINAL = "final"
EMPTY = "empty"
ENVELOPE = "envelope"


class WebAuthError(RuntimeError):
    """The DMS web session was rejected; the token secret must be renewed."""


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class WebSession:
    email: str
    tokenkey: str
    alias: str
    service_url: str = DEFAULT_SERVICE_URL

    def __post_init__(self) -> None:
        for name in ("email", "tokenkey", "alias", "service_url"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"DMS web session {name} must be a non-empty string")
        if not self.service_url.startswith("https://"):
            raise ValueError("DMS web service URL must use https")

    @classmethod
    def from_env(cls) -> "WebSession":
        return cls(
            email=os.environ.get("MOBIWORK_WEB_EMAIL", "").strip().replace("%40", "@"),
            tokenkey=os.environ.get("MOBIWORK_WEB_TOKENKEY", "").strip(),
            alias=os.environ.get("MOBIWORK_WEB_ALIAS", "").strip(),
            service_url=(os.environ.get("MOBIWORK_WEB_SERVICE_URL", "").strip()
                         or DEFAULT_SERVICE_URL).rstrip("/"),
        )

    @staticmethod
    def configured() -> bool:
        return all(os.environ.get(name, "").strip() for name in
                   ("MOBIWORK_WEB_EMAIL", "MOBIWORK_WEB_TOKENKEY", "MOBIWORK_WEB_ALIAS"))

    def headers(self) -> dict[str, str]:
        raw = f"{self.email}:{self.tokenkey}".encode("utf-8")
        return {
            "Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            "x-alias": self.alias,
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }

    def __repr__(self) -> str:  # never leak the token into logs
        return f"WebSession(email=<set>, alias=<set>, service_url={self.service_url!r})"


@dataclass(frozen=True)
class FetchPolicy:
    attempts: int = 4
    backoff_seconds: float = 20.0
    pause_seconds: float = 3.0
    timeout_seconds: float = 180.0

    @classmethod
    def from_env(cls) -> "FetchPolicy":
        def num(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            return float(raw) if raw else default

        policy = cls(
            attempts=int(num("PROMOTION_BONUS_UI_ATTEMPTS", cls.attempts)),
            backoff_seconds=num("PROMOTION_BONUS_UI_BACKOFF_SECONDS", cls.backoff_seconds),
            pause_seconds=num("PROMOTION_BONUS_UI_PAUSE_SECONDS", cls.pause_seconds),
            timeout_seconds=num("PROMOTION_BONUS_UI_TIMEOUT_SECONDS", cls.timeout_seconds),
        )
        if not 1 <= policy.attempts <= 10:
            raise ValueError("PROMOTION_BONUS_UI_ATTEMPTS must be between 1 and 10")
        if min(policy.backoff_seconds, policy.pause_seconds) < 0 or policy.timeout_seconds <= 0:
            raise ValueError("Promotion Bonus UI timings must be positive")
        return policy


# --------------------------------------------------------------------------- request


def report_period(start: str = "", end: str = "", today: date | None = None) -> tuple[date, date]:
    today = today or datetime.now(VN_TZ).date()
    first = date.fromisoformat(start) if start else today.replace(day=1)
    last = (date.fromisoformat(end) if end
            else today.replace(day=calendar.monthrange(today.year, today.month)[1]))
    if first > last:
        raise ValueError("Promotion Bonus from date must not be after to date")
    return first, last


def local_midnight_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=VN_TZ).timestamp() * 1000)


def report_query(orgid: str, first: date, last: date, sttt: str = "") -> dict[str, Any]:
    """Query string exactly as getFilteroption() builds it for an unfiltered search."""
    if not OBJECT_ID.fullmatch(orgid or ""):
        raise ValueError("Promotion Bonus UI request requires a 24-hex organization id")
    if sttt not in STTT_VALUES:
        raise ValueError(f"Unsupported sttt {sttt!r}; observed values: 0, 1, -1, 2")
    params: dict[str, Any] = {"orgid": orgid, "projectID": "", "projectName": "",
                              "assignTo": "", "eeName": "", "idcustomer": ""}
    if sttt:
        params["sttt"] = sttt
    params["startDate"] = local_midnight_ms(first)
    params["endDate"] = local_midnight_ms(last)
    return params


def classify(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("Promotion Bonus UI response must be a JSON object")
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        raise RuntimeError(f"DMS rejected Promotion Bonus request: {message.strip()[:200]}")
    rows = payload.get("result")
    if not isinstance(rows, list):
        raise ValueError("Promotion Bonus UI response is missing result array")
    if not rows:
        return EMPTY
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Promotion Bonus UI result contains non-object rows")
    envelopes = [isinstance(row.get("result"), list) and "RecordType" in row for row in rows]
    if all(envelopes):
        return ENVELOPE
    if any(envelopes):
        raise ValueError("Promotion Bonus UI result mixes envelopes and customer rows")
    return FINAL


@dataclass
class ProgramResult:
    program: dict[str, Any]
    status: str
    attempts: int
    rows: list[dict[str, Any]] = field(default_factory=list)
    targets: list[dict[str, Any]] = field(default_factory=list)
    rewards: list[dict[str, Any]] = field(default_factory=list)

    @property
    def program_id(self) -> str:
        return str(self.program.get("_id", ""))

    @property
    def program_name(self) -> str:
        return str(self.program.get("name", "")).strip()


def fetch_program(
    http: requests.Session,
    session: WebSession,
    program: dict[str, Any],
    params: dict[str, Any],
    policy: FetchPolicy,
    sleep: Callable[[float], None] = time.sleep,
) -> ProgramResult:
    program_id = str(program.get("_id", ""))
    if not OBJECT_ID.fullmatch(program_id):
        raise ValueError("Promotion Bonus program has no valid _id")
    url = f"{session.service_url}/PromotionBonusReport"
    status = ENVELOPE
    for attempt in range(1, policy.attempts + 1):
        response = http.post(url, params=params, json={"arrCT": [program_id]},
                             headers=session.headers(), timeout=policy.timeout_seconds)
        if response.status_code in (401, 403):
            raise WebAuthError(
                f"DMS web service returned HTTP {response.status_code}; renew the "
                "MOBIWORK_WEB_TOKENKEY secret from a signed-in DMS session"
            )
        response.raise_for_status()
        payload = response.json()
        status = classify(payload)
        if status != ENVELOPE:
            return ProgramResult(
                program=program, status=status, attempts=attempt,
                rows=list(payload["result"]) if status == FINAL else [],
                targets=[t for t in payload.get("arrChiTieu") or [] if isinstance(t, dict)],
                rewards=[t for t in payload.get("arrTraThuong") or [] if isinstance(t, dict)],
            )
        LOG.warning("Program %s returned order envelopes (attempt %s/%s); retrying",
                    program_id, attempt, policy.attempts)
        if attempt < policy.attempts:
            sleep(policy.backoff_seconds * attempt)
    return ProgramResult(program=program, status=status, attempts=policy.attempts)


def fetch_ui_snapshot(
    programs: list[dict[str, Any]],
    first: date,
    last: date,
    *,
    session: WebSession | None = None,
    policy: FetchPolicy | None = None,
    sttt: str = "",
    http: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[ProgramResult]:
    session = session or WebSession.from_env()
    policy = policy or FetchPolicy.from_env()
    orgids = {str(p.get("orgid", "")) for p in programs if p.get("orgid")}
    orgid = os.environ.get("MOBIWORK_ORG_ID", "").strip() or (orgids.pop() if len(orgids) == 1 else "")
    if not orgid and programs:
        raise ValueError("Cannot resolve a single DMS orgid from the programme catalogue; "
                         "set MOBIWORK_ORG_ID")
    params = report_query(orgid, first, last, sttt) if programs else {}
    http = http or requests.Session()
    results = []
    for index, program in enumerate(programs):
        if index:
            sleep(policy.pause_seconds)
        result = fetch_program(http, session, program, params, policy, sleep)
        LOG.info("Program %s/%s %s: %s rows=%s attempts=%s", index + 1, len(programs),
                 result.program_id, result.status, len(result.rows), result.attempts)
        results.append(result)
    return results


# --------------------------------------------------------------------------- maths


def _num(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else 0.0
    try:
        parsed = float(str(value).replace(",", "").strip())
    except ValueError:
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _rate(actual: float, plan: float) -> float:
    if plan == 0:
        return 0.0  # JS gives Infinity/NaN, both rendered as 0%
    return actual / plan * 100


def evaluate_range(actual: float, minimum: float, maximum: float) -> tuple[float, float]:
    """Còn lại / Tỷ lệ exactly as renderData() for min/max targets."""
    remaining = minimum - actual
    rate = _rate(actual, minimum)
    if maximum == 0:
        if actual > minimum:
            remaining, rate = 0.0, 100.0
    elif minimum <= actual < maximum:
        remaining, rate = 0.0, 100.0
    remaining = max(remaining, 0.0)
    if rate > 100 or rate < 0:
        rate = 0.0
    return remaining, rate


def evaluate_threshold(actual: float, plan: float) -> tuple[float, float]:
    """Còn lại / Tỷ lệ for per-product quantity and group-amount targets."""
    remaining = plan - actual
    rate = _rate(actual, plan)
    if actual >= plan:
        remaining, rate = 0.0, 100.0
    remaining = max(remaining, 0.0)
    if rate > 100 or rate < 0:
        rate = 0.0
    return remaining, rate


def _cell(target: str, item: str, plan: Any, minimum: Any, maximum: Any,
          actual: float, remaining: float, rate: float) -> dict[str, Any]:
    return {"Chỉ tiêu": target, "Hạng mục": item, "Kế hoạch": plan,
            "Mức tối thiểu": minimum, "Mức tối đa": maximum, "Thực hiện": actual,
            "Còn lại": remaining, "Tỷ lệ (%)": round(rate, 2), "Đạt": rate == 100.0}


def target_cells(row: dict[str, Any], targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One output cell per target column the Paybonus page renders for this row."""
    done = row.get("objThucHien") or {}
    planned = row.get("objChiTieu") or {}
    kind = str(row.get("type") or "")
    same_program = lambda t: str(t.get("_idCT", "")) == str(row.get("_idCT", ""))  # noqa: E731
    cells: list[dict[str, Any]] = []
    if not targets:
        return cells

    if "so_tien" in str(targets[0].get("_id", "")):
        for j, t in enumerate(targets):
            actual = _num(done.get(f"so_tien{j}")) if same_program(t) else 0.0
            minimum, maximum = _num(t.get("min")), _num(t.get("max"))
            cells.append(_cell(str(t.get("ten", "")), "", t.get("kh"), minimum, maximum,
                               actual, *evaluate_range(actual, minimum, maximum)))
    elif kind in {"MUTI_SP_SL_SP", "MUTI_SP_SL_TIEN", "MUTI_SP_SL_CKSP"}:
        for t in targets:
            tid = str(t.get("_id", ""))
            if planned.get(tid) is None:
                continue
            achieved = (done.get(tid) or {}).get("ct") if isinstance(done.get(tid), dict) else None
            if t.get("isSL") and isinstance(t.get("kh"), list):
                for item in t["kh"]:
                    if not isinstance(item, dict):
                        continue
                    plan = _num(item.get("sl"))
                    actual = _num((achieved or {}).get(str(item.get("_id", ""))) if isinstance(achieved, dict) else 0)
                    cells.append(_cell(str(t.get("ten", "")), str(item.get("ten", "")), plan, plan, 0,
                                       actual, *evaluate_threshold(actual, plan)))
            else:
                actual = _num(achieved)
                minimum, maximum = _num(t.get("min")), _num(t.get("max"))
                cells.append(_cell(str(t.get("ten", "")), "", t.get("kh"), minimum, maximum,
                                   actual, *evaluate_range(actual, minimum, maximum)))
    elif kind in {"GR_ST_MIN_SP", "GR_ST_MIN_TIEN", "GR_SL_MIN_SP", "GR_SL_MIN_TIEN"}:
        for t in targets:
            groups = t.get("sp") if isinstance(t.get("sp"), dict) else {}
            amounts = t.get("sotien") if isinstance(t.get("sotien"), dict) else {}
            tid = str(t.get("_id", ""))
            for key in groups:
                plan = _num(amounts.get(key))
                actual = _num((done.get(tid) or {}).get(key)) if same_program(t) and isinstance(done.get(tid), dict) else 0.0
                cells.append(_cell(str(t.get("ten", "")), str(key), plan, plan, 0,
                                   actual, *evaluate_threshold(actual, plan)))
    else:
        for t in targets:
            tid = str(t.get("_id", ""))
            actual = _num(done.get(tid)) if same_program(t) else 0.0
            minimum, maximum = _num(t.get("min")), _num(t.get("max"))
            cells.append(_cell(str(t.get("ten", "")), "", t.get("kh"), minimum, maximum,
                               actual, *evaluate_range(actual, minimum, maximum)))
    return cells


def reward_cells(row: dict[str, Any], rewards: list[dict[str, Any]]) -> list[tuple[str, float]]:
    given = row.get("objTraThuong") or {}
    out = []
    for reward in rewards:
        value = given.get(str(reward.get("_id", ""))) if isinstance(given, dict) else None
        if value is not None:
            out.append((str(reward.get("ten", "")), _num(value)))
    return out


# --------------------------------------------------------------------------- workbook


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("viewData", "name", "ten", "label", "value"):
            if value.get(key) not in (None, ""):
                return text(value[key])
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (text(v) for v in value) if part)
    return str(value).strip()


def _fmt_qty(value: float) -> str:
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"


CUSTOMER_COLUMNS = ["Chương trình", "STT", "Mã khách hàng", "Tên khách hàng", "SĐT",
                    "Địa chỉ", "Nhà phân phối", "Khu vực", "Loại khách hàng",
                    "Nhóm khách hàng", "Timepass", "Loại CT", "Số suất"]
TARGET_COLUMNS = ["Chỉ tiêu", "Hạng mục", "Kế hoạch", "Mức tối thiểu", "Mức tối đa",
                  "Thực hiện", "Còn lại", "Tỷ lệ (%)", "Đạt"]
SUMMARY_COLUMNS = CUSTOMER_COLUMNS + TARGET_COLUMNS + ["Kết quả", "Mã CT (id)"]
UNKNOWN_REGION = "Chưa xác định"
REWARD_EXTRA = ("Trưng bày yêu cầu", "Kết quả trưng bày", "Đủ điều kiện trả thưởng")


def build_records(results: list[ProgramResult]) -> tuple[list[dict], list[dict], list[dict]]:
    summary: list[dict[str, Any]] = []
    rewards_out: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for result in results:
        checks.append({"Chương trình": result.program_name, "Mã CT (id)": result.program_id,
                       "Trạng thái": result.status, "Số dòng khách hàng": len(result.rows),
                       "Số chỉ tiêu": len(result.targets), "Số sản phẩm thưởng": len(result.rewards),
                       "Số lần gọi": result.attempts})
        for stt, row in enumerate(result.rows, start=1):
            customer = {
                "Chương trình": result.program_name, "STT": stt,
                "Mã khách hàng": text(row.get("ma")), "Tên khách hàng": text(row.get("ten")),
                "SĐT": text(row.get("sdt")), "Địa chỉ": text(row.get("dc")),
                "Nhà phân phối": text(row.get("npp")),
                "Khu vực": text(row.get("kv")) or UNKNOWN_REGION,
                "Loại khách hàng": text(row.get("loai")), "Nhóm khách hàng": text(row.get("nhom")),
                "Timepass": text(row.get("timepass")), "Loại CT": text(row.get("type")),
                "Số suất": row.get("soSuatCT"),
            }
            extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
            customer.update({str(k): v for k, v in extra.items() if str(k) not in customer})
            given = reward_cells(row, result.rewards)
            outcome = "; ".join(f"{name}: {_fmt_qty(qty)}" for name, qty in given)
            for name, qty in given:
                rewards_out.append({**{k: customer[k] for k in ("Chương trình", "Mã khách hàng",
                                    "Tên khách hàng", "Nhà phân phối", "Khu vực")},
                                    "Sản phẩm thưởng": name, "Số lượng": qty,
                                    **{k: customer[k] for k in extra if k in REWARD_EXTRA},
                                    "Mã CT (id)": result.program_id})
            cells = target_cells(row, result.targets) or [{c: None for c in TARGET_COLUMNS}]
            for cell in cells:
                summary.append({**customer, **cell, "Kết quả": outcome,
                                "Mã CT (id)": result.program_id})
    return summary, rewards_out, checks


_INVALID_SHEET = re.compile(r"[\[\]:*?/\\]")
RESERVED_SHEETS = {"Tong_hop", "Ket_qua", "Kiem_tra"}


def sheet_name(label: str, used: set[str]) -> str:
    base = _INVALID_SHEET.sub("-", label).strip().strip("'")[:31] or UNKNOWN_REGION
    name, n = base, 2
    while name.casefold() in {u.casefold() for u in used}:
        suffix = f" ({n})"
        name, n = base[:31 - len(suffix)] + suffix, n + 1
    used.add(name)
    return name


def _extra_columns(records: list[dict[str, Any]], known: list[str]) -> list[str]:
    seen = dict.fromkeys(known)
    extra = [k for record in records for k in record if k not in seen]
    return list(dict.fromkeys(extra))


def build_ui_frames(results: list[ProgramResult], first: date, last: date,
                    sttt: str = "", source: str = "DMS Paybonus (gọi từng chương trình)",
                    notes: list[tuple[str, str]] | None = None) -> dict[str, pd.DataFrame]:
    summary, rewards, checks = build_records(results)
    columns = SUMMARY_COLUMNS[:-1] + _extra_columns(summary, SUMMARY_COLUMNS) + SUMMARY_COLUMNS[-1:]
    frame = pd.DataFrame(summary, columns=columns, dtype=object)
    reward_columns = ["Chương trình", "Mã khách hàng", "Tên khách hàng", "Nhà phân phối",
                      "Khu vực", "Sản phẩm thưởng", "Số lượng"]
    reward_columns += _extra_columns(rewards, reward_columns + ["Mã CT (id)"]) + ["Mã CT (id)"]
    frames: dict[str, pd.DataFrame] = {
        "Tong_hop": frame,
        "Ket_qua": pd.DataFrame(rewards, columns=reward_columns, dtype=object),
    }
    check = pd.DataFrame(checks, dtype=object)
    meta = pd.DataFrame([
        {"Chương trình": "Kỳ báo cáo", "Trạng thái": f"{first:%d/%m/%Y} - {last:%d/%m/%Y}"},
        {"Chương trình": "Cách tính", "Trạng thái": STTT_LABELS.get(sttt, sttt)},
        {"Chương trình": "Nguồn", "Trạng thái": source},
        *({"Chương trình": k, "Trạng thái": v} for k, v in (notes or [])),
    ], dtype=object)
    frames["Kiem_tra"] = pd.concat([meta, check], ignore_index=True)
    used = set(RESERVED_SHEETS)
    for region in sorted(frame["Khu vực"].dropna().unique(), key=lambda r: (r == UNKNOWN_REGION, r)):
        frames[sheet_name(str(region), used)] = frame[frame["Khu vực"] == region].reset_index(drop=True)
    return frames


def snapshot_counts(results: list[ProgramResult]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for result in results:
        by_status[result.status] = by_status.get(result.status, 0) + 1
    unresolved = [{"id": r.program_id, "name": r.program_name}
                  for r in results if r.status == ENVELOPE]
    return {"program_status_counts": by_status,
            "customer_row_count": sum(len(r.rows) for r in results),
            "programs_with_rows": sum(1 for r in results if r.rows),
            "unresolved_programs": unresolved}
