from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from excel_export import _format_sheet, _validate_excel_size
from mobiwork import MobiWorkClient
import promotion_bonus_ui as ui
from sharepoint_semantic import SemanticSharePointClient


LOG = logging.getLogger("mobiwork_promotion_bonus")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "promotion_bonus.json"
MANIFEST_PATH = Path("output") / "promotion_bonus_manifest.json"


@dataclass(frozen=True)
class PromotionBonusConfig:
    enabled: bool
    name: str
    folder: str
    filename: str
    catalog_url: str
    report_url: str
    catalog_page_size: int = 200
    # Code prefixes of programmes that accumulate over their whole period (e.g. "Thời gian
    # mua và tham gia tích lũy: 01/04 - 30/09") instead of per calendar month.
    cumulative_programs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all(isinstance(code, str) and code.strip() for code in self.cumulative_programs):
            raise ValueError("cumulative_programs must be non-empty strings")
        if type(self.enabled) is not bool:
            raise TypeError("enabled must be a boolean")
        if type(self.catalog_page_size) is not int:
            raise TypeError("catalog_page_size must be an integer")
        for field in ("name", "folder", "filename", "catalog_url", "report_url"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field).strip():
                raise ValueError(f"{field} must be a non-empty string")
        if any(part in {".", ".."} for part in self.folder.replace("\\", "/").split("/")):
            raise ValueError("folder must not contain traversal segments")
        if "/" in self.filename or "\\" in self.filename:
            raise ValueError("filename must be a basename")
        if self.catalog_page_size < 1 or self.catalog_page_size > 200:
            raise ValueError("catalog_page_size must be between 1 and 200")
        if not self.folder.strip():
            raise ValueError("Promotion Bonus folder must not be empty")
        if not self.filename.lower().endswith(".xlsx"):
            raise ValueError("Promotion Bonus filename must be an .xlsx workbook")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> PromotionBonusConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("config/promotion_bonus.json must contain a JSON object")
    if "cumulative_programs" in payload:
        payload = {**payload, "cumulative_programs": tuple(payload["cumulative_programs"])}
    return PromotionBonusConfig(**payload)


def _expect_object_list(payload: dict[str, Any], key: str, operation: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if value is None:
        raise ValueError(f"{operation}: response is missing {key!r}")
    if not isinstance(value, list):
        raise TypeError(f"{operation}: {key!r} must be an array")
    invalid = [type(item).__name__ for item in value if not isinstance(item, dict)]
    if invalid:
        raise TypeError(
            f"{operation}: {key!r} contains non-object values: "
            f"{', '.join(sorted(set(invalid)))}"
        )
    return value


def _optional_object_list(
    payload: dict[str, Any],
    key: str,
    operation: str,
) -> list[dict[str, Any]]:
    if payload.get(key) is None:
        return []
    return _expect_object_list(payload, key, operation)


def _api_total(payload: dict[str, Any], operation: str) -> int | None:
    value = payload.get("total")
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"{operation}: total must be an integer")
    try:
        total = int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{operation}: total is not an integer: {value!r}") from exc
    if total < 0:
        raise ValueError(f"{operation}: total must not be negative")
    return total


def _program_id(program: dict[str, Any]) -> str:
    value = program.get("_id")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def fetch_programs(
    client: MobiWorkClient,
    cfg: PromotionBonusConfig,
    *,
    filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch the complete PromotionBonus catalogue before requesting report snapshots."""
    records: list[dict[str, Any]] = []
    expected_total: int | None = None
    seen_pages: set[str] = set()
    page = 1

    while True:
        payload = client.get_json(
            cfg.catalog_url,
            {
                **(filters or {}),
                "page_size": cfg.catalog_page_size,
                "page_number": page,
            },
            operation_key="promotion_bonus_catalog",
            request_number=page,
        )
        page_total = _api_total(payload, "PromotionBonus catalogue")
        if expected_total is None:
            expected_total = page_total
        elif page_total is not None and page_total != expected_total:
            raise RuntimeError("PromotionBonus catalogue total changed during pagination")

        page_rows = _expect_object_list(payload, "data", "PromotionBonus catalogue")
        if page_rows:
            signature = json.dumps(
                page_rows,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            )
            if signature in seen_pages:
                raise RuntimeError(
                    f"PromotionBonus catalogue repeated page {page}; refusing incomplete pagination"
                )
            seen_pages.add(signature)

        records.extend(page_rows)
        if expected_total is not None and len(records) >= expected_total:
            break
        if not page_rows:
            break

        page += 1
        if page > 10_000:
            raise RuntimeError("PromotionBonus catalogue pagination safety limit exceeded")

    if expected_total is not None and len(records) != expected_total:
        raise RuntimeError(
            f"PromotionBonus catalogue API total={expected_total}, fetched={len(records)}"
        )

    unique: dict[str, dict[str, Any]] = {}
    for row_number, program in enumerate(records, start=1):
        program_id = _program_id(program)
        if not program_id:
            raise ValueError(
                f"PromotionBonus catalogue row {row_number} is missing required _id"
            )
        previous = unique.get(program_id)
        if previous is not None and previous != program:
            raise ValueError(
                f"PromotionBonus catalogue has conflicting duplicate _id={program_id}"
            )
        unique[program_id] = program
    if expected_total is not None and len(unique) != expected_total:
        raise RuntimeError(
            f"PromotionBonus catalogue API total={expected_total}, unique programs={len(unique)}"
        )
    return list(unique.values())


def _with_program_provenance(
    rows: list[dict[str, Any]],
    program: dict[str, Any],
) -> list[dict[str, Any]]:
    program_id = _program_id(program)
    program_name = str(program.get("name", "")).strip()
    return [
        {
            **row,
            "promotion_program_id": program_id,
            "promotion_program_name": program_name,
        }
        for row in rows
    ]


def fetch_snapshot(
    client: MobiWorkClient,
    cfg: PromotionBonusConfig,
    programs: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Fetch one authoritative current snapshot per program.

    The report endpoint permits up to five ids but does not document a stable field that
    maps every returned row back to one id. One request per program preserves exact
    provenance and avoids mixing reward rows between programs.
    """
    data_rows: list[dict[str, Any]] = []
    target_rows: list[dict[str, Any]] = []
    reward_rows: list[dict[str, Any]] = []

    for request_number, program in enumerate(programs, start=1):
        program_id = _program_id(program)
        if not program_id:
            raise ValueError("PromotionBonus report cannot run without a program _id")

        payload = client.get_json(
            cfg.report_url,
            {"id_ct": program_id},
            operation_key="promotion_bonus_report",
            request_number=request_number,
        )
        current_data = _expect_object_list(
            payload,
            "data",
            f"PromotionBonusReport id_ct={program_id}",
        )
        expected_total = _api_total(payload, f"PromotionBonusReport id_ct={program_id}")
        if expected_total is not None and len(current_data) != expected_total:
            raise RuntimeError(
                f"PromotionBonusReport id_ct={program_id}: "
                f"API total={expected_total}, fetched={len(current_data)}"
            )

        current_targets = _optional_object_list(
            payload,
            "arrChiTieu",
            f"PromotionBonusReport id_ct={program_id}",
        )
        current_rewards = _optional_object_list(
            payload,
            "arrTraThuong",
            f"PromotionBonusReport id_ct={program_id}",
        )

        data_rows.extend(_with_program_provenance(current_data, program))
        target_rows.extend(_with_program_provenance(current_targets, program))
        reward_rows.extend(_with_program_provenance(current_rewards, program))

    return {
        "programs": programs,
        "data": data_rows,
        "targets": target_rows,
        "rewards": reward_rows,
    }


def _excel_safe(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


def _frame(records: list[dict[str, Any]], label: str) -> pd.DataFrame:
    def flatten(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in record.items():
            column = f"{prefix}_{key}" if prefix else str(key)
            cells = flatten(value, column) if isinstance(value, dict) and value else {column: value}
            if result.keys() & cells.keys():
                raise ValueError(f"{label}: nested column collision")
            result.update(cells)
        return result

    frame = pd.DataFrame([flatten(row) for row in records], dtype=object)
    if not frame.empty:
        for column in frame.columns:
            if frame[column].map(lambda value: isinstance(value, (dict, list, tuple, set))).any():
                frame[column] = frame[column].map(_excel_safe)
    if len(frame.columns) > 16_384:
        raise ValueError(f"{label}: exceeds Excel column limit")
    for column in frame.columns:
        if len(str(column)) > 32_767:
            raise ValueError(f"{label}: column name exceeds Excel cell limit")
        for value in frame[column]:
            if isinstance(value, str) and len(value) > 32_767:
                raise ValueError(f"{label}: value exceeds Excel cell limit")
    _validate_excel_size(frame, label)
    return frame


def build_frames(snapshot: dict[str, list[dict[str, Any]]]) -> dict[str, pd.DataFrame]:
    return {
        "ChuongTrinh": _frame(snapshot["programs"], "ChuongTrinh"),
        "Data": _frame(snapshot["data"], "Data"),
        "ChiTieu": _frame(snapshot["targets"], "ChiTieu"),
        "TraThuong": _frame(snapshot["rewards"], "TraThuong"),
    }


def write_workbook(
    frames: dict[str, pd.DataFrame],
    filename: str,
    output_dir: Path = Path("output"),
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".xlsx", delete=False) as handle:
        staged = Path(handle.name)
    try:
        with pd.ExcelWriter(staged, engine="openpyxl") as writer:
            for sheet_name, frame in frames.items():
                frame.to_excel(writer, sheet_name=sheet_name, index=False)
                # Source strings are data, including values beginning with '='.
                for row in writer.sheets[sheet_name].iter_rows():
                    for cell in row:
                        if cell.data_type == "f":
                            cell.data_type = "s"
                _format_sheet(writer, sheet_name)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)
    return path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _write_manifest(payload: dict[str, Any]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


SOURCES = {"auto", "calc", "ui", "openapi"}


def resolve_source() -> str:
    """auto -> compute from OpenAPI data (PromotionBonus + Bill + DisplayData).

    ``ui`` reads the DMS web page (needs web session secrets) and ``openapi`` keeps
    the legacy raw PromotionBonusReport snapshot.
    """
    requested = os.environ.get("PROMOTION_BONUS_SOURCE", "auto").strip().casefold() or "auto"
    if requested not in SOURCES:
        raise ValueError(f"PROMOTION_BONUS_SOURCE must be one of {sorted(SOURCES)}")
    return "calc" if requested == "auto" else requested


def _select_programs(programs: list[dict[str, Any]], selected: str) -> list[dict[str, Any]]:
    selected = (selected or "all").strip()
    if selected == "all":
        return programs
    wanted = {part.strip() for part in selected.split(",") if part.strip()}
    chosen = [p for p in programs if _program_id(p) in wanted]
    missing = wanted - {_program_id(p) for p in chosen}
    if missing:
        raise ValueError(f"Selected Promotion Bonus program(s) not in catalogue: {sorted(missing)}")
    return chosen


def _build_ui_workbook(
    client: MobiWorkClient,
    cfg: PromotionBonusConfig,
    manifest: dict[str, Any],
) -> Path:
    first, last = ui.report_period(
        os.environ.get("PROMOTION_BONUS_FROM_DATE", "").strip(),
        os.environ.get("PROMOTION_BONUS_TO_DATE", "").strip(),
    )
    sttt = os.environ.get("PROMOTION_BONUS_STTT", "").strip()
    filters = {"fromdate": first.strftime("%d/%m/%Y"), "todate": last.strftime("%d/%m/%Y")}
    programs = _select_programs(
        fetch_programs(client, cfg, filters=filters),
        os.environ.get("PROMOTION_BONUS_PROGRAM", "all"),
    )
    manifest.update({"from_date": first.isoformat(), "to_date": last.isoformat(),
                     "sttt": sttt, "program_count": len(programs)})
    results = ui.fetch_ui_snapshot(programs, first, last, sttt=sttt)
    counts = ui.snapshot_counts(results)
    manifest.update(counts)
    if counts["unresolved_programs"] and not _env_bool("PROMOTION_BONUS_ALLOW_PARTIAL", False):
        raise RuntimeError(
            f"{len(counts['unresolved_programs'])} program(s) still returned order envelopes "
            "after retries; refusing to publish an incomplete report"
        )
    manifest["phase"] = "workbook_build"
    return write_workbook(ui.build_ui_frames(results, first, last, sttt), cfg.filename)


def _bill_detail(first: Any, dry_run: bool, manifest: dict[str, Any]) -> pd.DataFrame:
    """Bill monthly master ChiTietSP for the report month (local file in dry runs)."""
    from io import BytesIO

    from data_cham_anh_export import _monthly_master_path
    from main import load_reports
    from monthly_master import master_filename

    bill = next(r for r in load_reports(ROOT / "config" / "reports.json") if r.key == "bill" and r.enabled)
    local = Path("output") / master_filename(bill.name, first)
    if dry_run and local.exists():
        content = local.read_bytes()
        manifest["bill_source"] = f"local:{local.name}"
    else:
        sharepoint = SemanticSharePointClient.from_env()
        drive = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip() or sharepoint.get_drive_id(
            sharepoint.get_site_id())
        remote = _monthly_master_path(bill, first)
        content = sharepoint.download_file_bytes(drive, remote)
        if not content:
            raise ValueError(f"Bill monthly master missing: {remote}")
        manifest["bill_source"] = f"sharepoint:{remote}"
    manifest["bill_sha256"] = hashlib.sha256(content).hexdigest()
    return pd.read_excel(BytesIO(content), sheet_name="ChiTietSP", dtype=object)


_BILL_CACHE: dict[str, Any] = {}


def _cached_bill(first: Any, dry_run: bool) -> pd.DataFrame | None:
    """Bill master of a month, read once per run; None when the month has no master."""
    key = f"{first:%Y-%m}"
    if key not in _BILL_CACHE:
        try:
            _BILL_CACHE[key] = _bill_detail(first, dry_run, {})
        except ValueError:
            _BILL_CACHE[key] = None
    return _BILL_CACHE[key]


def _program_day(program: dict[str, Any], field: str) -> Any:
    try:
        return datetime.fromtimestamp(int(program[field]) / 1000, ui.VN_TZ).date()
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _program_mode(program: dict[str, Any], cfg: PromotionBonusConfig,
                  overrides: dict[str, dict[str, Any]] | None = None) -> str:
    """'cumulative' | 'monthly' | '' (multi-month programme whose method is not declared).

    Declared in BoSung_Mapping (sheet ChuongTrinh) or ``cumulative_programs``; names saying
    "THEO THÁNG" and programmes inside one calendar month are monthly.
    """
    import bosung_mapping as bosung
    import promotion_bonus_calc as calc

    name = str(program.get("name", "")).strip()
    declared = (overrides or {}).get(calc.program_prefix(name), {}).get("Cách tính")
    if declared and bosung.program_mode(declared):
        return bosung.program_mode(declared)
    if any(name.casefold().startswith(code.strip().casefold()) for code in cfg.cumulative_programs):
        return "cumulative"
    start, end = _program_day(program, "startDate"), _program_day(program, "endDate")
    if not start or not end or (start.year, start.month) == (end.year, end.month):
        return "monthly"
    return "monthly" if "theo tháng" in name.casefold() else ""


def _is_cumulative(program: dict[str, Any], cfg: PromotionBonusConfig, first: Any,
                   overrides: dict[str, dict[str, Any]] | None = None) -> bool:
    start = _program_day(program, "startDate")
    return bool(start and start < first and _program_mode(program, cfg, overrides) == "cumulative")


def _program_todo_rows(undeclared: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Programmes the business must look at: undeclared multi-month method, or not computable."""
    import promotion_bonus_calc as calc

    rows: list[dict[str, Any]] = []
    for program in undeclared:
        name = str(program.get("name", "")).strip()
        start, end = _program_day(program, "startDate"), _program_day(program, "endDate")
        rows.append({"_sheet": "ChuongTrinh", "Mã CT": calc.program_prefix(name), "Tên CT": name,
                     "Thời gian": f"{start:%d/%m/%Y} - {end:%d/%m/%Y}" if start and end else "",
                     "Còn thiếu": ["Cách tính"],
                     "Nguyên nhân": "CT kéo dài nhiều tháng, chưa khai cách tính (đang tính theo từng tháng)",
                     "Gợi ý": "Theo thông báo CT: tích lũy suốt thời gian CT → 'Tích lũy cả kỳ'; "
                              "xét từng tháng → 'Theo tháng'"})
    for issue in issues:
        name = str(issue.get("Chương trình") or "").strip()
        rows.append({"_sheet": "ChuongTrinh", "Mã CT": calc.bonus_code(name), "Tên CT": name,
                     "Còn thiếu": ["Không tính được"], "Nguyên nhân": str(issue.get("Vấn đề", ""))[:250],
                     "Gợi ý": "Quy tắc CT trên DMS chưa được hỗ trợ: cần bổ sung code tính"})
    return rows


def _period_lines(start: Any, last: Any, dry_run: bool, missing: set[str]) -> list[dict[str, Any]]:
    import promotion_bonus_calc as calc

    lines: list[dict[str, Any]] = []
    cursor = start.replace(day=1)
    while cursor <= last:
        detail = _cached_bill(cursor, dry_run)
        if detail is None:
            missing.add(f"{cursor:%Y-%m}")
        else:
            lines.extend(calc.sold_lines(detail, max(cursor, start), min(_month_end(cursor), last)))
        cursor = _month_end(cursor) + timedelta(days=1)
    return lines


def _with_bill_identity(customers: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Fill code for buyers missing from the catalogue using the Bill line itself."""
    merged = {key: dict(value) for key, value in customers.items()}
    for line in lines:
        if line["customer"] and line["code"]:
            merged.setdefault(line["customer"], {}).setdefault("customer_code", line["code"])
    return merged


def _github_notice(title: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title={title}::{message}")


def _load_customers(client: MobiWorkClient, manifest: dict[str, Any]) -> tuple[dict[str, Any], str]:
    from customer_catalogue import enrich_customer_config

    try:  # enrich_customer_config already retries live-change races per window
        catalogue = enrich_customer_config(client, {"customer_catalogue_start_date": "01/01/1900"})
    except ValueError as exc:
        manifest.setdefault("customer_catalogue_retries", []).append(str(exc))
        LOG.warning("Customer catalogue failed: %s", exc)
        manifest["customer_catalogue_count"] = 0
        return {}, ("Không tải ổn định được danh mục khách hàng; mã/tên lấy từ đơn hàng, "
                    "khách chưa có đơn hiển thị theo ID")
    customers = catalogue["customer_catalogue"]
    manifest["customer_catalogue_count"] = catalogue["customer_catalogue_audit"]["count"]
    return customers, f"Danh mục khách hàng OpenAPI ({len(customers)} khách)"


def _calc_month_frames(
    client: MobiWorkClient,
    cfg: PromotionBonusConfig,
    first: Any,
    last: Any,
    dry_run: bool,
    customers: dict[str, Any],
    customer_note: str,
    manifest: dict[str, Any],
    verbose: bool = True,
    detail_config: dict[str, Any] | None = None,
) -> dict[str, pd.DataFrame]:
    """Compute one calendar month; ``manifest`` receives that month's counts."""
    import promotion_bonus_calc as calc
    if (first.year, first.month) != (last.year, last.month):
        raise ValueError("Computed Promotion Bonus report covers one calendar month at a time")
    if os.environ.get("PROMOTION_BONUS_STTT", "").strip() not in {"", "0"}:
        raise ValueError("Computed Promotion Bonus supports only sttt=0 (price × quantity)")
    if detail_config is None:
        detail_config = _load_template_config(client)
    filters = {"fromdate": first.strftime("%d/%m/%Y"), "todate": last.strftime("%d/%m/%Y")}
    programs = _select_programs(
        fetch_programs(client, cfg, filters=filters),
        os.environ.get("PROMOTION_BONUS_PROGRAM", "all"),
    )
    manifest.update({"from_date": first.isoformat(), "to_date": last.isoformat(),
                     "program_count": len(programs)})
    lines = calc.sold_lines(_bill_detail(first, dry_run, manifest), first, last)
    manifest["sold_line_count"] = len(lines)
    # Whole-period accumulation (configured programmes): sales from the programme start.
    program_overrides = detail_config.get("program_overrides", {})
    cumulative = [p for p in programs if _is_cumulative(p, cfg, first, program_overrides)]
    undeclared = [p for p in programs if not _program_mode(p, cfg, program_overrides)]
    groups: dict[Any, list[dict[str, Any]]] = {}
    for program in cumulative:
        groups.setdefault(_program_day(program, "startDate"), []).append(program)
    missing_months: set[str] = set()
    period_lines = {start: _period_lines(start, last, dry_run, missing_months) for start in groups}
    all_lines = lines + [line for group in period_lines.values() for line in group]
    if cumulative:
        manifest["cumulative_programs"] = {str(p.get("name"))[:60]: f"{_program_day(p, 'startDate')}"
                                           for p in cumulative}
        manifest["cumulative_missing_months"] = sorted(missing_months)
        if verbose:
            _github_notice(f"Promotion Bonus cumulative {first:%m/%Y}",
                           json.dumps({"programs": len(cumulative),
                                       "from": sorted(str(s) for s in groups),
                                       "lines": {str(k): len(v) for k, v in period_lines.items()},
                                       "months_without_orders": sorted(missing_months)},
                                      ensure_ascii=False))
    customer_map = _with_bill_identity(customers, all_lines)
    displays: dict[tuple[str, str], str] = {}
    display_note = "Không có chương trình yêu cầu trưng bày"
    if any(isinstance(p.get("cttb"), dict) and p["cttb"].get("ten") for p in programs):
        try:
            records = calc.fetch_display_records(client, first, last)
            displays = calc.display_passes(records)
            summary = calc.display_summary(records, programs)
            manifest["display_summary"] = summary
            if verbose:
                _github_notice("Promotion Bonus DisplayData", json.dumps(summary, ensure_ascii=False)[:3000])
            display_note = f"DisplayData {len(records)} lượt chấm"
            manifest["display_record_count"] = len(records)
        except Exception as exc:  # display data is informative; never block the report
            display_note = f"Không lấy được DisplayData ({type(exc).__name__})"
            manifest["display_error"] = f"{type(exc).__name__}: {exc}"
    sales_metadata, conflicts = calc.customer_sales_metadata(all_lines, detail_config.get("employees", {}),
                                                              detail_config)
    manifest["customer_assignment_conflicts"] = conflicts
    results, issues = calc.compute([p for p in programs if p not in cumulative], lines, customer_map,
                                   displays=displays, sales_metadata=sales_metadata)
    for start, group in groups.items():
        group_results, group_issues = calc.compute(group, period_lines[start], customer_map,
                                                   displays=displays, sales_metadata=sales_metadata)
        for program, result in zip(group, group_results, strict=True):
            end = _program_day(program, "endDate")
            if end and end > last:
                calc.provisional([result], end)
        results.extend(group_results)
        issues.extend(group_issues)
        ends = {_program_day(p, "endDate") for p in group}
        if verbose or any(end and first <= end <= last for end in ends):
            summary = []
            for item in calc.diagnostics(group_results):
                region = item["name"].rsplit("(", 1)[-1].rstrip(")") if "(" in item["name"] else ""
                summary.append(f"{calc.bonus_code(item['name'])} ({region})|đăng ký {item['registered']}|"
                               f"có doanh số {item['with_sales']}|đạt {item['reached']}|trả {item['eligible']}")
            _github_notice(f"Promotion Bonus lũy kế từ {start:%d/%m/%Y} đến {last:%d/%m/%Y}", " ; ".join(summary))
    program_rows = _program_todo_rows(undeclared, issues)
    over_quota = []
    for result in results:  # "Số suất quyết toán": more eligible customers than slots
        try:
            quota = int(str(result.program.get("soSuat") or "").strip())
        except ValueError:
            continue
        paid = sum(1 for row in result.rows if row.get("_eligible") == "Có")
        if quota > 0 and paid > quota:
            over_quota.append(f"{calc.bonus_code(result.program_name)}: {paid}/{quota}")
            program_rows.append({"_sheet": "ChuongTrinh", "Mã CT": calc.bonus_code(result.program_name),
                                 "Tên CT": result.program_name, "Còn thiếu": ["Vượt số suất"],
                                 "Nguyên nhân": f"{paid} khách đủ điều kiện, CT chỉ có {quota} suất",
                                 "Gợi ý": "Duyệt danh sách trả thưởng theo thứ tự ưu tiên của CT"})
    manifest["over_quota"] = over_quota
    counts = ui.snapshot_counts(results)
    diag = calc.diagnostics(results)
    manifest.update(counts)
    manifest.update({"rule_issues": issues, "program_diagnostics": diag,
                     "reached_rows": sum(d["reached"] for d in diag),
                     "eligible_rows": sum(d["eligible"] for d in diag),
                     "review_display_rows": sum(d["review_display"] for d in diag)})
    _github_notice(f"Promotion Bonus {first:%m/%Y}",
                   f"programs={len(programs)} rows={counts['customer_row_count']} "
                   f"reached={manifest['reached_rows']} eligible={manifest['eligible_rows']} "
                   f"review_display={manifest['review_display_rows']} "
                   f"sold_lines={len(lines)} display={display_note}")
    if verbose:
        lines_out = [f"{item['name'][:28]}|r{item['registered']}|s{item['with_sales']}|"
                     f"d{item['reached']}|tb{item['display_passed']}|e{item['eligible']}"
                     for item in diag if item["registered"]]
        for start in range(0, len(lines_out), 20):  # GitHub keeps only 10 notices per step
            _github_notice(f"Promotion Bonus programs {start + 1}-{start + len(lines_out[start:start + 20])}",
                           " ; ".join(lines_out[start:start + 20]))
    notes = [
        ("Quy tắc", "Khách đăng ký của chương trình; đơn bán (bỏ dòng khuyến mãi) có ngày giao trong kỳ; "
                    "chỉ cộng sản phẩm và đúng đơn vị khai trong chương trình; tiền = đơn giá × số lượng"),
        ("Khu vực", "Danh mục khách nếu có; nếu không, dùng vùng/NPP duy nhất từ nhân viên trên đơn bán "
                    "và cây phòng ban hiện tại. Thiếu hoặc nhiều giá trị thì để chưa xác định. "
                    "Vùng áp dụng CT được ghi riêng, không thay cho khu vực khách."),
        ("Trưng bày", display_note + ". Khách đạt doanh số ở chương trình có trưng bày nhưng chưa có "
                      "kết quả 'Đạt' được ghi 'Cần kiểm tra trưng bày'; quà chỉ nằm ở Thưởng dự kiến, "
                      "chưa đưa vào Ket_qua hoặc dòng TRẢ THƯỞNG"),
        ("Khách hàng", customer_note + ". Danh sách khách đăng ký là danh sách hiện tại của chương trình"),
        ("Đơn bán hàng", f"{len(lines)} dòng bán trong kỳ ({manifest.get('bill_source', '')})"),
    ]
    if over_quota:
        notes.append(("Số suất", "Vượt số suất quyết toán (đủ điều kiện/số suất): " + "; ".join(over_quota)))
    if cumulative:
        notes.append(("Tích lũy nhiều tháng",
                      f"{len(cumulative)} chương trình tính lũy kế từ ngày bắt đầu CT đến hết kỳ báo cáo "
                      "(cấu hình cumulative_programs). Trước tháng kết thúc CT: 'Tạm tính', chưa trả thưởng. "
                      + (f"Tháng chưa có dữ liệu đơn hàng: {', '.join(sorted(missing_months))}"
                         if missing_months else "")))
    notes.append(("Sheet BaoCao", "Theo mẫu Báo cáo chi tiết CTKM theo KH: mỗi dòng hàng được tính vào chương "
                                  "trình (quy đổi KÉT/THÙNG/BÌNH); khách đủ điều kiện có thêm dòng 'TRẢ THƯỞNG' ghi quà. "
                                  "Thực hiện ở Tong_hop tính theo đơn vị khai trong chương trình"))
    summary_frames = ui.build_ui_frames(results, first, last, "",
                                        "Tính từ OpenAPI (PromotionBonus + Đơn bán hàng)", notes)
    report, detail_issues = _template_report(client, customers, results, first, detail_config)
    frames: dict[str, pd.DataFrame] = {"BaoCao": report}
    for name in ("Tong_hop", "Ket_qua", "Kiem_tra"):
        frames[name] = summary_frames[name]
    if not detail_issues.empty:
        from promotion_detail import OPTIONAL_MAPPING_FIELDS, UNIT_GAP

        detail_issues = detail_issues.assign(**{"Mức độ": detail_issues["Trường"].map(
            lambda field: "Thiếu thông tin mô tả" if field in OPTIONAL_MAPPING_FIELDS else "Chặn xuất bản")})
        unit_gaps = detail_issues["Lý do"].astype(str).str.startswith(UNIT_GAP)
        detail_issues.loc[unit_gaps, "Mức độ"] = "Thiếu quy đổi đơn vị"
        manifest["unit_gaps"] = int(unit_gaps.sum())
        frames["CanBoSung"] = detail_issues
    manifest["programs_need_method"] = len(undeclared)
    manifest["programs_not_computed"] = len(issues)
    if program_rows and (verbose or (first.year, first.month) == (datetime.now(ui.VN_TZ).year,
                                                                 datetime.now(ui.VN_TZ).month)):
        _github_notice(f"Chương trình cần xem {first:%m/%Y}",
                       f"{len({calc.program_prefix(str(p.get('name', ''))) for p in undeclared})} CT nhiều tháng "
                       f"chưa khai cách tính; {len(issues)} CT không tính được – xem sheet ChuongTrinh "
                       "trong 08_BoSungDanhMuc/CanBoSung_TongHop.xlsx")
    if _bosung_enabled(detail_config):
        import bosung_mapping as bosung

        label = f"TraThuong {first:%Y-%m}"
        TODO_UPDATES[label] = bosung.aggregate(bosung.todo_rows(detail_issues, detail_config) + program_rows, label)
    if issues:
        frames["Can_xem"] = pd.DataFrame(issues, dtype=object)
    manifest["template_rows"] = len(report)
    manifest["template_issues"] = len(detail_issues)
    from promotion_detail import blocking_issue_count

    manifest["blocking_issues"] = blocking_issue_count(detail_issues)
    missing = (detail_issues["Trường"].value_counts().head(6).to_dict()
               if not detail_issues.empty and "Trường" in detail_issues else {})
    manifest["missing_fields"] = missing
    manifest["quality_status"] = "needs_review" if (
        issues or not detail_issues.empty or conflicts or manifest["review_display_rows"]
        or manifest.get("display_error") or not customers) else "complete_supported_rules"
    manifest["dms_equivalence_verified"] = False
    _github_notice(f"Promotion Bonus template {first:%m/%Y}",
                   f"rows={len(report)} issues={len(detail_issues)} missing={missing}")
    if manifest["blocking_issues"] and not dry_run:
        raise ValueError("Promotion Bonus has invalid source values, identities or conversions; "
                         "run dry-run to inspect CanBoSung. Nothing published.")
    return frames


# Fill-in rows per month ("TraThuong YYYY-MM") for 08_BoSungDanhMuc/CanBoSung_TongHop.xlsx.
TODO_UPDATES: dict[str, list[dict[str, Any]]] = {}


def _bosung_enabled(cfg: dict[str, Any] | None = None) -> bool:
    return (os.environ.get("BOSUNG_MAPPING", "").strip().casefold() == "true"
            and (cfg is None or bool(cfg.get("bosung_mapping", False))))


def _load_template_config(client: MobiWorkClient) -> dict[str, Any]:
    """Enrich once per run; a failed lookup is retried on the next run."""
    from promotion_detail import enrich_product_config
    from promotion_detail import load_config as load_detail_config

    cfg = load_detail_config()
    if cfg.get("fetch_product_catalogue", False):
        try:
            cfg = enrich_product_config(client, cfg)
        except Exception as exc:
            LOG.warning("Product catalogue unavailable for template: %s", exc)
    if cfg.get("fetch_sales_structure", False):
        from sales_structure import enrich_employee_config
        try:
            cfg = enrich_employee_config(client, cfg)
        except Exception as exc:
            LOG.warning("Sales structure unavailable for template: %s", exc)
    if _bosung_enabled(cfg):
        import bosung_mapping as bosung

        cfg = bosung.apply_overrides(cfg, bosung.load_overrides())
    return cfg


def _template_report(
    client: MobiWorkClient,
    customers: dict[str, Any],
    results: list[Any],
    first: Any,
    detail_config: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Render counted sale lines and rewards with the CTKM detail template mappings."""
    import promotion_bonus_calc as calc
    from promotion_detail import COLUMNS, build_report

    cfg = dict(_load_template_config(client) if detail_config is None else detail_config)
    cfg["customer_catalogue"] = customers
    sources = calc.detail_source(results, f"{first:%m/%Y}")
    # Keep full bonus codes (e.g. "008/TB/GT/01/2026_Q4 - Mức 2"); the CTKM normaliser
    # would otherwise shorten them.
    cfg["program_codes"] = {**cfg.get("program_codes", {}), **{code: code for code, _ in sources}}
    reports, issues = [], []
    for code, source in sources:
        report, issue = build_report(source, cfg)
        reports.append(report)
        if not issue.empty:
            issues.append(issue.assign(**{"Mã CTKM": code}))
    report = pd.concat(reports, ignore_index=True) if reports else pd.DataFrame(columns=COLUMNS)
    return report.reindex(columns=COLUMNS), (pd.concat(issues, ignore_index=True) if issues
                                             else pd.DataFrame())


TEMPLATE_TITLE = "BÁO CÁO TRẢ THƯỞNG CHI TIẾT THEO KHÁCH HÀNG"


def _write_bonus(frames: dict[str, pd.DataFrame], filename: str, first: Any) -> Path:
    from promotion_workbook import write_detail_workbook

    return write_detail_workbook(frames, filename, first, title=f"{TEMPLATE_TITLE} - THÁNG {first:%m/%Y}")


def _monthly_target(cfg: PromotionBonusConfig, first: Any) -> tuple[str, str]:
    return f"BaoCaoTraThuong_{first:%Y-%m}.xlsx", f"{cfg.folder}/{first:%Y}/{first:%m}"


def _history_months(raw: str, dry_run: bool) -> list[Any]:
    """Explicit YYYY-MM list, or every month that has a Bill monthly master."""
    from datetime import date

    if raw.strip().casefold() != "all_existing":
        months = []
        for part in raw.split(","):
            part = part.strip()
            if part:
                year, month = part.split("-")
                months.append(date(int(year), int(month), 1))
        if not months:
            raise ValueError("PROMOTION_BONUS_MONTHS must list YYYY-MM months or all_existing")
        return sorted(set(months))
    if dry_run:
        raise ValueError("all_existing needs SharePoint access; use explicit months in dry runs")
    from main import load_reports
    from promotion_months import discover_bill_months

    bill = next(r for r in load_reports(ROOT / "config" / "reports.json") if r.key == "bill" and r.enabled)
    sharepoint = SemanticSharePointClient.from_env()
    drive = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip() or sharepoint.get_drive_id(sharepoint.get_site_id())
    today = datetime.now(ui.VN_TZ).date()
    current = today.replace(day=1)
    # The current month is kept fresh by the regular sync; history covers closed months.
    return sorted(m.replace(day=1) for m in discover_bill_months(sharepoint, drive, bill, today)
                  if m.replace(day=1) != current)


def _month_end(first: Any) -> Any:
    import calendar

    return first.replace(day=calendar.monthrange(first.year, first.month)[1])


def _build_calc_workbook(
    client: MobiWorkClient,
    cfg: PromotionBonusConfig,
    manifest: dict[str, Any],
    dry_run: bool,
) -> tuple[Path, str, list[tuple[Path, str]]]:
    """Return (primary workbook, its folder, extra uploads).

    Current month -> BaoCaoTraThuong_Current.xlsx plus the monthly archive copy.
    Past months (explicit dates, or PROMOTION_BONUS_MONTHS) -> monthly files only, so
    a backfill never overwrites the current snapshot.
    """
    history = os.environ.get("PROMOTION_BONUS_MONTHS", "").strip()
    today = datetime.now(ui.VN_TZ).date()
    if not history:
        first, last = ui.report_period(
            os.environ.get("PROMOTION_BONUS_FROM_DATE", "").strip(),
            os.environ.get("PROMOTION_BONUS_TO_DATE", "").strip(),
        )
        if (first.year, first.month) != (last.year, last.month):
            raise ValueError("Computed Promotion Bonus report covers one calendar month at a time")
    customers, customer_note = _load_customers(client, manifest)
    detail_config = _load_template_config(client)
    if history:
        manifest["storage_mode"] = "monthly_history"
        outputs: list[tuple[Path, str]] = []
        manifest["months"] = {}
        for first in _history_months(history, dry_run):
            last = _month_end(first)
            month_manifest: dict[str, Any] = {}
            manifest["months"][f"{first:%Y-%m}"] = month_manifest
            try:
                frames = _calc_month_frames(client, cfg, first, last, dry_run, customers,
                                            customer_note, month_manifest, verbose=False,
                                            detail_config=detail_config)
            except Exception as exc:  # one missing month must not hide the others
                month_manifest["error"] = f"{type(exc).__name__}: {exc}"
                _github_notice(f"Promotion Bonus {first:%m/%Y} skipped", month_manifest["error"][:300])
                continue
            filename, folder = _monthly_target(cfg, first)
            outputs.append((_write_bonus(frames, filename, first), folder))
        if not outputs:
            raise RuntimeError("No Promotion Bonus month could be computed")
        manifest["phase"] = "workbook_build"
        return outputs[0][0], outputs[0][1], outputs[1:]

    frames = _calc_month_frames(client, cfg, first, last, dry_run, customers, customer_note, manifest,
                               detail_config=detail_config)
    manifest["phase"] = "workbook_build"
    filename, folder = _monthly_target(cfg, first)
    monthly = _write_bonus(frames, filename, first)
    if (first.year, first.month) != (today.year, today.month):
        manifest["storage_mode"] = "monthly_history"
        return monthly, folder, []
    extras = [(monthly, folder)]
    previous_days = int(os.environ.get("PROMOTION_BONUS_PREVIOUS_DAYS", "0") or 0)
    if previous_days and today.day <= previous_days:
        # Early in a month, keep refreshing last month's file: late deliveries, back-dated
        # edits, the monthly order-master rebuild, and programmes that ended last month.
        previous = (first - timedelta(days=1)).replace(day=1)
        previous_manifest: dict[str, Any] = {}
        manifest["previous_month"] = previous_manifest
        try:
            previous_frames = _calc_month_frames(client, cfg, previous, _month_end(previous), dry_run,
                                                 customers, customer_note, previous_manifest,
                                                 verbose=False, detail_config=detail_config)
            previous_file, previous_folder = _monthly_target(cfg, previous)
            extras.append((_write_bonus(previous_frames, previous_file, previous), previous_folder))
        except Exception as exc:  # never block the current month
            previous_manifest["error"] = f"{type(exc).__name__}: {exc}"
            _github_notice(f"Promotion Bonus {previous:%m/%Y} not refreshed", previous_manifest["error"][:300])
    return _write_bonus(frames, cfg.filename, first), cfg.folder, extras


def run() -> dict[str, Any]:
    dry_run = _env_bool("DRY_RUN", False)
    started_at = datetime.now(timezone.utc)
    manifest: dict[str, Any] = {
        "status": "running",
        "dataset": "promotion_bonus",
        "storage_mode": "current_snapshot",
        "historical_backfill_supported": False,
        "dry_run": dry_run,
        "phase": "config",
        "workbook_published": False,
        "started_at": started_at.isoformat(),
    }

    try:
        source = resolve_source()
        manifest["source"] = source
        manifest["historical_backfill_supported"] = source == "calc"
        if source == "openapi" and _env_bool("PROMOTION_BONUS_REQUIRE_DMS_MATCH", False):
            raise RuntimeError(
                "DMS-equivalent export blocked: report date parameters, calculation enum "
                "and region/customer schema have not been verified. "
                "Configure MOBIWORK_WEB_EMAIL/TOKENKEY/ALIAS to use the DMS web source."
            )
        cfg = load_config()
        manifest.update({"folder": cfg.folder, "filename": cfg.filename})
        if not cfg.enabled:
            manifest.update({
                "status": "skipped",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "reason": "config disabled",
            })
            _write_manifest(manifest)
            return manifest

        manifest["phase"] = "source_fetch"
        client = MobiWorkClient.from_env()
        extra_uploads: list[tuple[Path, str]] = []
        primary_folder = cfg.folder
        if source == "calc":
            path, primary_folder, extra_uploads = _build_calc_workbook(client, cfg, manifest, dry_run)
        elif source == "ui":
            path = _build_ui_workbook(client, cfg, manifest)
        else:
            programs = fetch_programs(client, cfg)
            snapshot = fetch_snapshot(client, cfg, programs)
            manifest["phase"] = "workbook_build"
            frames = build_frames(snapshot)
            path = write_workbook(frames, cfg.filename)
            manifest.update(
                {
                    "program_count": len(programs),
                    "report_request_count": len(programs),
                    "data_row_count": len(snapshot["data"]),
                    "target_row_count": len(snapshot["targets"]),
                    "reward_row_count": len(snapshot["rewards"]),
                }
            )
        content = path.read_bytes()
        manifest.update(
            {
                "workbook_sha256": hashlib.sha256(content).hexdigest(),
                "workbook_bytes": len(content),
            }
        )

        if not dry_run:
            manifest["phase"] = "workbook_publish"
            sharepoint = SemanticSharePointClient.from_env()
            drive_id = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
            if not drive_id:
                site_id = sharepoint.get_site_id()
                drive_id = sharepoint.get_drive_id(site_id)

            history = manifest.get("storage_mode") == "monthly_history"
            uploaded: dict[str, Any] = {}
            failures: list[str] = []
            targets = [(path, primary_folder, not history)] + [(p, f, False) for p, f in extra_uploads]
            for target_path, target_folder, required in targets:
                try:
                    result = sharepoint.upload_file(drive_id, target_path, target_folder)
                except Exception as exc:
                    # A workbook someone has open in Excel is locked (HTTP 423). The current
                    # snapshot must publish; archive/history copies are retried next run.
                    if required:
                        raise
                    failures.append(f"{target_folder}/{target_path.name}: {type(exc).__name__}: {exc}"[:300])
                    if os.environ.get("GITHUB_ACTIONS") == "true":
                        print(f"::warning title=Promotion Bonus file not updated::{failures[-1]}")
                    continue
                uploaded = uploaded or result
                manifest.setdefault("published_files", []).append(f"{target_folder}/{target_path.name}")
            if failures:
                manifest["publish_failures"] = failures
            if not manifest.get("published_files"):
                raise RuntimeError("No Promotion Bonus workbook could be published: " + "; ".join(failures))
            manifest["workbook_published"] = True
            manifest.update(
                {
                    "sharepoint_write_avoided": bool(uploaded.get("upload_skipped")),
                    "verification_mode": uploaded.get("verification_mode"),
                    "semantic_match": uploaded.get("semantic_match"),
                    "web_url": uploaded.get("webUrl"),
                }
            )

        if TODO_UPDATES and _bosung_enabled():
            import bosung_mapping as bosung

            manifest["bosung"] = bosung.publish(dict(TODO_UPDATES), None if dry_run else sharepoint,
                                                "" if dry_run else drive_id, dry_run=dry_run)
        manifest.update(
            {
                "status": "success",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if not dry_run:
            manifest["phase"] = "state_publish"
            sharepoint.upload_json(
                drive_id,
                f"{cfg.folder}/_sync_state/"
                + ("promotion_bonus_history.json" if manifest.get("storage_mode") == "monthly_history"
                   else "promotion_bonus.json"),
                {**manifest, "phase": "complete"},
            )
        manifest["phase"] = "complete"
        _write_manifest(manifest)
        LOG.info(
            "Promotion Bonus snapshot complete source=%s programs=%s rows=%s",
            source,
            manifest.get("program_count"),
            manifest.get("customer_row_count", manifest.get("data_row_count")),
        )
        return manifest
    except Exception as exc:
        manifest.update(
            {
                "status": "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        _write_manifest(manifest)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            detail = " ".join(f"{type(exc).__name__}: {exc}".split())[:500]
            print(f"::error title=Promotion Bonus failed ({manifest.get('phase')})::{detail}")
        raise


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run()
