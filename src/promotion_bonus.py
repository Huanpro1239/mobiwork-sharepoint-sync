from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from report_runtime import env_bool as _env_bool
from api_contract import api_total as _api_total, expect_object_list as _expect_object_list, optional_object_list as _optional_object_list
from excel_export import records_frame as _frame, write_workbook
from promotion_catalogue import PromotionBonusConfig, fetch_programs, load_config, program_id as _program_id
from report_context import CatalogueCache, enrich_cached
from report_runtime import write_manifest
from mobiwork import MobiWorkClient
import promotion_bonus_ui as ui
from sharepoint_semantic import SemanticSharePointClient


LOG = logging.getLogger("mobiwork_promotion_bonus")
ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path("output") / "promotion_bonus_manifest.json"


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


def build_frames(snapshot: dict[str, list[dict[str, Any]]]) -> dict[str, pd.DataFrame]:
    return {
        "ChuongTrinh": _frame(snapshot["programs"], "ChuongTrinh"),
        "Data": _frame(snapshot["data"], "Data"),
        "ChiTieu": _frame(snapshot["targets"], "ChiTieu"),
        "TraThuong": _frame(snapshot["rewards"], "TraThuong"),
    }


def _write_manifest(payload: dict[str, Any]) -> None:
    write_manifest(MANIFEST_PATH, payload)


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
    return bool(start and _program_mode(program, cfg, overrides) == "cumulative")


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
                     "Nguyên nhân": "CT kéo dài nhiều tháng, chưa khai cách tính (chưa xác nhận trả thưởng)",
                     "Gợi ý": "Theo thông báo CT: tích lũy suốt thời gian CT → 'Tích lũy cả kỳ'; "
                              "xét từng tháng → 'Theo tháng'"})
    for issue in issues:
        name = str(issue.get("Chương trình") or "").strip()
        rows.append({"_sheet": "ChuongTrinh", "Mã CT": calc.bonus_code(name), "Tên CT": name,
                     "Còn thiếu": ["Không tính được"], "Nguyên nhân": str(issue.get("Vấn đề", ""))[:250],
                     "Gợi ý": "Quy tắc CT trên DMS chưa được hỗ trợ: cần bổ sung code tính"})
    return rows


def _period_lines(start: Any, last: Any, dry_run: bool, missing: set[str],
                  unit_config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    import promotion_bonus_calc as calc

    lines: list[dict[str, Any]] = []
    cursor = start.replace(day=1)
    while cursor <= last:
        detail = _cached_bill(cursor, dry_run)
        if detail is None:
            missing.add(f"{cursor:%Y-%m}")
        else:
            lines.extend(calc.sold_lines(detail, max(cursor, start), min(_month_end(cursor), last), unit_config))
        cursor = _month_end(cursor) + timedelta(days=1)
    return lines


def _with_bill_identity(customers: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Fill absent identity fields from unambiguous values on the customer's Bills."""
    merged = {key: dict(value) for key, value in customers.items()}
    candidates: dict[str, dict[str, set[str]]] = {}
    fields = {"customer_code": "ma_kh", "Tên Khách hàng": "ten_kh",
              "Địa chỉ": "dia_chi", "Số ĐT": "sdt"}
    for line in lines:
        identity = line["customer"]
        if not identity:
            continue
        metadata = candidates.setdefault(identity, {})
        for target, source in fields.items():
            value = ui.text((line.get("raw") or {}).get(source))
            if target == "customer_code":
                value = ui.text(line.get("code"))
            if value:
                metadata.setdefault(target, set()).add(value)
    for identity, fields in candidates.items():
        metadata = merged.setdefault(identity, {})
        for field, values in fields.items():
            if not ui.text(metadata.get(field)) and len(values) == 1:
                metadata[field] = next(iter(values))
    return merged


def _github_warning(title: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title={title}::{' '.join(message.split())[:3800]}")


def _github_notice(title: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title={title}::{message}")


def _load_customers(client: MobiWorkClient, manifest: dict[str, Any],
                    detail_config: dict[str, Any] | None = None,
                    cache: CatalogueCache | None = None) -> tuple[dict[str, Any], str]:
    from customer_catalogue import enrich_customer_config

    try:  # enrich_customer_config already retries live-change races per window
        catalogue = enrich_cached(cache, "customers", client, detail_config or {}, enrich_customer_config)
    except ValueError as exc:
        manifest.setdefault("customer_catalogue_retries", []).append(str(exc))
        LOG.warning("Customer catalogue failed: %s", exc)
        manifest["customer_catalogue_count"] = 0
        return {}, ("Không tải ổn định được danh mục khách hàng; mã/tên lấy từ đơn hàng, "
                    "khách chưa có đơn hiển thị theo ID")
    customers = catalogue["customer_catalogue"]
    manifest["customer_catalogue_count"] = catalogue["customer_catalogue_audit"]["count"]
    manifest["customer_catalogue"] = catalogue["customer_catalogue_audit"]
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
    manifest["allow_blank_fields"] = detail_config.get("allow_blank_fields", [])
    filters = {"fromdate": first.strftime("%d/%m/%Y"), "todate": last.strftime("%d/%m/%Y")}
    programs = _select_programs(
        fetch_programs(client, cfg, filters=filters),
        os.environ.get("PROMOTION_BONUS_PROGRAM", "all"),
    )
    manifest.update({"from_date": first.isoformat(), "to_date": last.isoformat(),
                     "program_count": len(programs)})
    bill_detail = _bill_detail(first, dry_run, manifest)
    _BILL_CACHE[f"{first:%Y-%m}"] = bill_detail
    lines = calc.sold_lines(bill_detail, first, last, detail_config)
    manifest["sold_line_count"] = len(lines)
    # Whole-period accumulation (configured programmes): sales from the programme start.
    program_overrides = detail_config.get("program_overrides", {})
    cumulative = [p for p in programs if _is_cumulative(p, cfg, first, program_overrides)]
    undeclared = [p for p in programs if not _program_mode(p, cfg, program_overrides)]
    groups: dict[Any, list[dict[str, Any]]] = {}
    for program in cumulative:
        groups.setdefault(_program_day(program, "startDate"), []).append(program)
    missing_months: set[str] = set()
    period_lines, missing_by_start = {}, {}
    for start in groups:
        missing: set[str] = set()
        period_lines[start] = _period_lines(start, last, dry_run, missing, detail_config)
        missing_by_start[start] = missing
        missing_months.update(missing)
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
            records, by_programme = calc.fetch_display_for_programs(client, programs, first, last)
            displays = calc.display_passes(records)
            summary = calc.display_summary(records, programs)
            summary["by_programme"] = by_programme
            manifest["display_summary"] = summary
            if verbose:
                _github_notice("Promotion Bonus DisplayData", json.dumps(summary, ensure_ascii=False)[:3000])
            display_note = f"DisplayData {len(records)} lượt chấm"
            manifest["display_record_count"] = len(records)
        except Exception as exc:  # display data is informative; never block the report
            display_note = f"Không lấy được DisplayData ({type(exc).__name__})"
            manifest["display_error"] = f"{type(exc).__name__}: {exc}"
        confirmed = detail_config.get("display_overrides") or {}
        for key, result in confirmed.items():  # BoSung_Mapping sheet TrungBay wins over DMS
            code, _, name = key.partition("|")
            displays[(code, name)] = calc.DISPLAY_FAIL if result == "Không đạt" else result
        if confirmed:
            display_note += f"; {len(confirmed)} kết quả trưng bày nhập tay (BoSung_Mapping/TrungBay)"
            manifest["display_overrides"] = len(confirmed)
    sales_metadata, conflicts = calc.customer_sales_metadata(all_lines, detail_config.get("employees", {}),
                                                              detail_config)
    manifest["customer_assignment_conflicts"] = conflicts
    results, issues = calc.compute([p for p in programs if p not in cumulative], lines, customer_map,
                                   displays=displays, sales_metadata=sales_metadata)
    for result in results:
        if result.program in undeclared:
            calc.hold_rewards([result], "Chưa xác định cách tính theo tháng/cả kỳ")
    for start, group in groups.items():
        group_results, group_issues = calc.compute(group, period_lines[start], customer_map,
                                                   displays=displays, sales_metadata=sales_metadata)
        for program, result in zip(group, group_results, strict=True):
            end = _program_day(program, "endDate")
            if missing_by_start[start]:
                calc.hold_rewards([result], "Thiếu dữ liệu kỳ tích lũy: " + ", ".join(sorted(missing_by_start[start])))
            elif end and end > last:
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
    unit_gaps = calc.unit_gap_summary(results)
    manifest["unit_gap_lines"] = unit_gaps["missing_unit_line_count"]
    manifest["unit_gap_held_rows"] = unit_gaps["held_customer_rows"]
    if unit_gaps["missing_unit_line_count"]:
        _github_warning(f"Dòng đơn thiếu ĐVT {first:%m/%Y}",
                        f"{unit_gaps['held_customer_rows']} khách × CT tạm giữ, chưa trả thưởng. "
                        + json.dumps(unit_gaps["lines"], ensure_ascii=False))
        for gap in unit_gaps["lines"]:
            for code in gap["programmes"]:
                program_rows.append({"_sheet": "ChuongTrinh", "Mã CT": code, "Tên CT": code,
                                     "Còn thiếu": ["ĐVT dòng đơn"],
                                     "Nguyên nhân": f"Đơn {gap['order']} dòng {gap['line']} SP {gap['sku']} "
                                                    f"(SL {gap['quantity']:g}) không ghi ĐVT; khách tạm giữ",
                                     "Gợi ý": "Sửa ĐVT trên đơn DMS, hoặc khai sale_unit_defaults / "
                                              "line_unit_overrides trong config/promotion_detail.json"})
    for start, missing in missing_by_start.items():
        if missing:
            program_rows.extend({"_sheet": "ChuongTrinh", "Mã CT": calc.program_prefix(str(p.get("name", ""))),
                                 "Tên CT": p.get("name", ""), "Còn thiếu": ["Dữ liệu đơn hàng"],
                                 "Nguyên nhân": "Thiếu tháng đơn hàng: " + ", ".join(sorted(missing)),
                                 "Gợi ý": "Tải/bổ sung DonBanHang của các tháng thiếu rồi chạy lại báo cáo"}
                                for p in groups[start])
    over_quota = []
    quota_summary = {}
    for result in results:  # "Số suất" = programme total, allocated first-come
        quota = calc.program_quota(result.program)
        if not quota:
            continue
        program = result.program
        used, unknown = 0, []
        start = _program_day(program, "startDate")
        if start and start < first and program not in cumulative:
            # Monthly programme: slots paid in earlier months of the programme are used up.
            cursor = start.replace(day=1)
            while cursor < first:
                detail = _cached_bill(cursor, dry_run)
                if detail is None:
                    unknown.append(f"{cursor:%Y-%m}")
                else:
                    month_lines = calc.sold_lines(detail, max(cursor, start), _month_end(cursor), detail_config)
                    previous, _ = calc.compute([program], month_lines, customer_map, displays=displays)
                    used += calc.apply_quota(previous[0], quota, used)["paid"]
                cursor = _month_end(cursor) + timedelta(days=1)
        stats = calc.apply_quota(result, quota, used)
        code = calc.bonus_code(result.program_name)
        quota_summary[code] = {**stats, "months_without_orders": unknown}
        if stats["cut"] or stats["reduced"] or unknown:
            over_quota.append(f"{code}: {stats['used_before'] + stats['paid']}/{quota}, "
                              f"hết suất {stats['cut']} khách")
            program_rows.append({"_sheet": "ChuongTrinh", "Mã CT": code, "Tên CT": result.program_name,
                                 "Còn thiếu": ["Hết số suất"],
                                 "Nguyên nhân": f"CT có {quota} suất, đã dùng {stats['used_before']} ở tháng "
                                                f"trước; {stats['cut']} khách đạt sau không còn suất"
                                                + (f"; thiếu đơn tháng {', '.join(unknown)}" if unknown else ""),
                                 "Gợi ý": "Suất chia theo ngày đạt chỉ tiêu; muốn trả thêm thì tăng "
                                          "số suất CT trên DMS"})
    manifest["quota"] = quota_summary
    for result in results:  # customers waiting only for the display result → sheet TrungBay
        for row in result.rows:
            if row.get("_eligible") != calc.REVIEW_DISPLAY:
                continue
            extra = row.get("extra") or {}
            program_rows.append({"_sheet": "TrungBay", "Mã Khách hàng": row.get("ma"),
                                 "Chương trình trưng bày": extra.get("Trưng bày yêu cầu"),
                                 "Tên Khách hàng": row.get("ten"), "Mã CT": calc.bonus_code(result.program_name),
                                 "Còn thiếu": ["Kết quả"],
                                 "Nguyên nhân": "Đạt doanh số; DMS chưa trả kết quả chấm trưng bày "
                                                f"({extra.get('Kết quả trưng bày') or 'không có dữ liệu'})",
                                 "Gợi ý": "Điền Kết quả = Đạt / Không đạt"})
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
    if any(m.get("_province_source") for m in customers.values()):
        notes.append(("Nguồn tỉnh", "Ưu tiên tinh_thanh_moi; nếu trống dùng tỉnh cũ hoặc địa danh "
                      "ở cuối địa chỉ DMS khi khớp danh mục. Giữ địa danh nguồn, chưa quy đổi địa giới mới."))
    notes.append(("Số suất", "Số suất (soSuat) là tổng suất của cả CT, mỗi bội số = 1 suất, chia theo ngày khách "
                             "đạt chỉ tiêu (sớm trước); CT theo tháng trừ suất đã trả ở các tháng trước; "
                             "gioiHanCT = false hoặc để trống là không giới hạn. Khách đạt sau khi hết suất ghi "
                             f"'{calc.QUOTA_OUT}'." + (" Hết suất (đã dùng/tổng): " + "; ".join(over_quota)
                                                        if over_quota else "")))
    if undeclared:
        notes.append(("Cách tính còn thiếu", f"{len(undeclared)} mức CT nhiều tháng chưa khai cách tính. "
                      "Doanh số theo kỳ báo cáo và thưởng dự kiến vẫn hiển thị; chưa xác nhận trả thưởng. "
                      "Khai cách tính trong BoSung_Mapping.xlsx, sheet ChuongTrinh."))
    if cumulative:
        notes.append(("Tích lũy nhiều tháng",
                      f"{len(cumulative)} chương trình tính lũy kế từ ngày bắt đầu CT đến hết kỳ báo cáo "
                      "(cấu hình cumulative_programs). Trước tháng kết thúc CT: 'Tạm tính', chưa trả thưởng. "
                      + (f"Tháng chưa có dữ liệu đơn hàng: {', '.join(sorted(missing_months))}"
                         if missing_months else "")))
    notes.append(("Sheet BaoCao", "Theo mẫu Báo cáo chi tiết CTKM theo KH: mỗi dòng hàng được tính vào chương "
                                  "trình (quy đổi KÉT/THÙNG/BÌNH); khách đủ điều kiện có thêm dòng 'TRẢ THƯỞNG' ghi quà. "
                                  "Thực hiện ở Tong_hop tính theo đơn vị khai trong chương trình"))
    notes.append(("Tiền thưởng từng đơn",
                  "Khách đủ điều kiện (Có): quà/tiền của khách được chia cho từng dòng đơn đã tính vào CT theo "
                  "tỷ lệ đóng góp (doanh số với CT theo tiền, số lượng với CT theo số lượng); tổng các dòng "
                  "bằng dòng TRẢ THƯỞNG. Tiền thưởng của quà hiện vật = số lượng quà × giá bán bình quân của "
                  "chính sản phẩm/ĐVT đó trong kỳ; không có giá bán thì để trống. Khách 'Tạm tính' hoặc "
                  "'Cần kiểm tra trưng bày' chưa phân bổ."))
    notes.extend([
        ("Tiền mặt và quà", "TraThuong: từng khách × CT × khoản thưởng, gồm dự kiến và đủ điều kiện. "
                            "Đủ điều kiện là kết quả tính, chưa xác nhận đã chi tiền/giao quà. "
                            "BaoCao: cột Z chỉ là số lượng quà; tiền mặt có cột riêng. "
                            "Tiền mặt phân bổ và giá trị quà ước tính tách riêng; cột Tiền thưởng phân bổ (đ) "
                            "giữ tổng hai khoản để tương thích, để trống nếu chưa có giá quà."),
        ("Khuyến mãi trên đơn", "KhuyenMaiDonHang dùng tháng ngày đơn hàng như báo cáo CTKM; "
                               "trả thưởng dùng ngày giao hàng. Mỗi dòng bán xuất một lần với tất cả mã CT "
                               "của đơn, hàng tặng theo CT trực tiếp; không cộng lại vào thưởng tính toán. "
                               "Chiết khấu SP nguồn giữ giá trị DMS, không coi là tiền mặt trả thưởng."),
        ("Phạm vi chương trình", "ChuongTrinh gồm toàn bộ mức CT trả thưởng API chọn cho kỳ báo cáo "
                                 "và CTKM có trên đơn tháng đó. CT không có khách/doanh số hoặc chưa tính được "
                                 "vẫn hiển thị. Không phải danh mục CTKM chưa phát sinh trên đơn. "
                                 "Voucher là quà theo mã nguồn; không suy giá tiền từ tên quà."),
        ("Theo dõi tích lũy", "TheoDoiTichLuy theo mẫu khách tham gia: một khách/mức CT là 1 suất "
                              "theo xác nhận ngày 09/10/2026; ngày đăng ký và người liên hệ thiếu nguồn để trống. "
                              "TỔNG TÍCH LŨY là tổng từ đầu CT; CÒN LẠI là số âm chưa đạt mục tiêu tối thiểu "
                              "của kỳ xét thưởng (tháng hoặc cả kỳ). Số suất đạt chỉ phản ánh ngưỡng doanh số, "
                              "chưa phải số suất đã trả thưởng. Theo tháng: mỗi tháng xét riêng; cả kỳ: "
                              "số suất tháng là chênh lệch số suất lũy kế, có thể âm khi điều chỉnh hoặc vượt "
                              "giới hạn trên. Tháng ngoài CT, tương lai hoặc thiếu nguồn để trống; "
                              "tổng chưa đủ nguồn để trống kèm lý do. Danh sách tham gia là danh sách hiện tại."),
    ])
    summary_frames = ui.build_ui_frames(results, first, last, "",
                                        "Tính từ OpenAPI (PromotionBonus + Đơn bán hàng)", notes)
    report, detail_issues = _template_report(client, customers, results, first, detail_config,
                                             calc.average_prices(all_lines))
    from promotion_tracking import tracking_report

    tracking = tracking_report(results, first, last, lambda month: _cached_bill(month, dry_run),
                               customer_map, detail_config,
                               {r.program_id: _program_mode(r.program, cfg, program_overrides) for r in results})
    manifest["tracking_report"] = {"rows": len(tracking), "months": tracking.attrs["months"],
                                   "missing_months": tracking.attrs["missing_months"],
                                   "source_warning_rows": int(tracking["Thông tin nguồn"].ne("").sum()),
                                   "slots_per_customer_level": 1}
    frames: dict[str, pd.DataFrame] = {"TheoDoiTichLuy": tracking}
    from promotion_reward_report import invoice_promotions, program_coverage, reward_ledger

    invoice_cfg = {**detail_config, "customer_catalogue": customers}
    invoice_report, invoice_issues, invoice_programs = invoice_promotions(bill_detail, invoice_cfg, first)
    frames["KhuyenMaiDonHang"] = invoice_report
    frames["TraThuong"] = ledger = reward_ledger(results, first)
    frames["BaoCao"] = report
    frames["Thuong_theo_don"] = reward_by_order(report)
    frames["ChuongTrinh"] = pd.concat([program_coverage(results, first, issues), invoice_programs],
                                       ignore_index=True)
    if not invoice_issues.empty:
        detail_issues = pd.concat([detail_issues, invoice_issues.assign(Nguồn="KhuyenMaiDonHang")],
                                  ignore_index=True)
    manifest["reward_coverage"] = {
        "bonus_program_levels": len(results), "invoice_programs": len(invoice_programs),
        "cash_program_levels": int((frames["ChuongTrinh"]["Loại thưởng"] == "Tiền mặt").sum()),
        "cash_eligible_vnd": float(ledger["Tiền mặt đủ điều kiện (đ)"].sum()),
        "cash_proposed_vnd": float(ledger["Tiền mặt dự kiến (đ)"].sum()),
        "gift_reward_rows": int((ledger["Số lượng quà đủ điều kiện"] > 0).sum()),
        "invoice_rows": len(invoice_report), "invoice_gift_rows": invoice_report.attrs["gift_rows"],
        "invoice_issues": len(invoice_issues),
    }
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
    calculation_issues = list(issues)
    calculation_issues.extend({"Chương trình": p["Tên CT"], "Vấn đề": p["Nguyên nhân"]}
                              for p in program_rows if p["_sheet"] == "ChuongTrinh")
    if calculation_issues:
        frames["Can_xem"] = pd.DataFrame(calculation_issues, dtype=object).drop_duplicates()
    manifest["template_rows"] = len(report)
    manifest["template_issues"] = len(detail_issues)
    from promotion_detail import blocking_issue_count

    manifest["blocking_issues"] = blocking_issue_count(detail_issues)
    missing = (detail_issues["Trường"].value_counts().head(6).to_dict()
               if not detail_issues.empty and "Trường" in detail_issues else {})
    manifest["missing_fields"] = missing
    manifest["quality_status"] = "needs_review" if (
        calculation_issues or not detail_issues.empty or conflicts or manifest["review_display_rows"]
        or manifest.get("display_error") or not customers
        or manifest["tracking_report"]["source_warning_rows"]) else "complete_supported_rules"
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


def _load_template_config(client: MobiWorkClient, cache: CatalogueCache | None = None) -> dict[str, Any]:
    """Enrich once per run; a failed lookup is retried on the next run."""
    from promotion_detail import enrich_product_config
    from promotion_detail import load_config as load_detail_config

    cfg = load_detail_config()
    if cfg.get("fetch_product_catalogue", False):
        try:
            cfg = enrich_cached(cache, "products", client, cfg, enrich_product_config)
        except Exception as exc:
            LOG.warning("Product catalogue unavailable for template: %s", exc)
    if cfg.get("fetch_sales_structure", False):
        from sales_structure import enrich_employee_config
        try:
            cfg = enrich_cached(cache, "employees", client, cfg, enrich_employee_config)
        except Exception as exc:
            LOG.warning("Sales structure unavailable for template: %s", exc)
    if _bosung_enabled(cfg):
        import bosung_mapping as bosung

        overrides = bosung.load_overrides() if cache is None else cache.get("overrides", bosung.load_overrides)
        cfg = bosung.apply_overrides(cfg, overrides)
    return cfg


def _template_report(
    client: MobiWorkClient,
    customers: dict[str, Any],
    results: list[Any],
    first: Any,
    detail_config: dict[str, Any] | None = None,
    prices: dict[tuple[str, str], float] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Render counted sale lines and rewards with the CTKM detail template mappings,
    plus the reward allocated to each order line."""
    import promotion_bonus_calc as calc
    from promotion_detail import BONUS_COLUMNS, COLUMNS, build_report

    columns = COLUMNS + list(BONUS_COLUMNS)
    cfg = dict(_load_template_config(client) if detail_config is None else detail_config)
    cfg["customer_catalogue"] = customers
    sources = calc.detail_source(results, f"{first:%m/%Y}", prices)
    # Keep full bonus codes (e.g. "008/TB/GT/01/2026_Q4 - Mức 2"); the CTKM normaliser
    # would otherwise shorten them.
    cfg["program_codes"] = {**cfg.get("program_codes", {}), **{code: code for code, _ in sources}}
    reports, issues = [], []
    for code, source in sources:
        report, issue = build_report(source, cfg, BONUS_COLUMNS)
        reports.append(report)
        if not issue.empty:
            issues.append(issue.assign(**{"Mã CTKM": code}))
    report = pd.concat(reports, ignore_index=True) if reports else pd.DataFrame(columns=columns)
    return report.reindex(columns=columns), (pd.concat(issues, ignore_index=True) if issues
                                             else pd.DataFrame())


ORDER_KEYS = ["Mã CTKM", "Vùng", "Tỉnh", "Tên NPP", "Mã Khách hàng", "Tên Khách hàng",
              "Ngày Đơn hàng", "Mã Đơn hàng"]


def reward_by_order(report: pd.DataFrame) -> pd.DataFrame:
    """One row per order × programme with the reward allocated to that order."""
    import promotion_bonus_calc as calc
    from promotion_detail import BONUS_COLUMNS

    text_col, value_col = list(BONUS_COLUMNS)[:2]
    amounts = [value_col, "Tiền mặt phân bổ (đ)", "Giá trị quà ước tính (đ)"]
    columns = ORDER_KEYS + ["Số dòng hàng", "THÀNH TIỀN", text_col] + amounts
    if report.empty or text_col not in report:
        return pd.DataFrame(columns=columns)
    paid = report[(report["Mã Đơn hàng"] != calc.GIFT_ORDER)
                  & (report[text_col].notna() | report[value_col].notna())].copy()
    if paid.empty:
        return pd.DataFrame(columns=columns)
    keys = paid[ORDER_KEYS].astype(object).where(paid[ORDER_KEYS].notna(), "")
    paid[ORDER_KEYS] = keys
    rows = []
    for key, group in paid.groupby(ORDER_KEYS, sort=True, dropna=False):
        totals = {}
        for col in amounts:
            values = pd.to_numeric(group[col] if col in group else pd.Series([None]), errors="coerce")
            totals[col] = None if values.isna().any() else float(values.sum())
        rows.append({**dict(zip(ORDER_KEYS, key, strict=True)), "Số dòng hàng": len(group),
                     "THÀNH TIỀN": pd.to_numeric(group["THÀNH TIỀN"], errors="coerce").sum(),
                     text_col: "; ".join(t for t in group[text_col].dropna().astype(str) if t) or None,
                     **totals})
    return pd.DataFrame(rows, columns=columns)


TEMPLATE_TITLE = "BÁO CÁO TRẢ THƯỞNG CHI TIẾT THEO KHÁCH HÀNG"


def _write_bonus(frames: dict[str, pd.DataFrame], filename: str, first: Any) -> Path:
    from promotion_workbook import write_detail_workbook

    return write_detail_workbook(frames, filename, first, title=f"{TEMPLATE_TITLE} - THÁNG {first:%m/%Y}")


def _monthly_target(cfg: PromotionBonusConfig, first: Any) -> tuple[str, str]:
    return f"BaoCaoTraThuong_{first:%Y-%m}.xlsx", f"{cfg.folder}/{first:%Y}/{first:%m}"


def _history_months(raw: str, dry_run: bool) -> list[Any]:
    """Explicit YYYY-MM list, or every month that has a Bill monthly master."""
    if raw.strip().casefold() != "all_existing":
        from promotion_months import parse_requested_months

        return parse_requested_months(raw, datetime.now(ui.VN_TZ).date())
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
    cache: CatalogueCache | None = None,
) -> tuple[Path, str, list[tuple[Path, str]]]:
    """Return (primary workbook, its folder, extra uploads).

    Current month -> BaoCaoTraThuong_Current.xlsx plus the monthly archive copy.
    Past months (explicit dates, or PROMOTION_BONUS_MONTHS) -> monthly files only, so
    a backfill never overwrites the current snapshot.
    """
    _BILL_CACHE.clear()
    history = os.environ.get("PROMOTION_BONUS_MONTHS", "").strip()
    today = datetime.now(ui.VN_TZ).date()
    if not history:
        first, last = ui.report_period(
            os.environ.get("PROMOTION_BONUS_FROM_DATE", "").strip(),
            os.environ.get("PROMOTION_BONUS_TO_DATE", "").strip(),
        )
        if (first.year, first.month) != (last.year, last.month):
            raise ValueError("Computed Promotion Bonus report covers one calendar month at a time")
    detail_config = _load_template_config(client, cache) if cache is not None else _load_template_config(client)
    manifest["allow_blank_fields"] = detail_config.get("allow_blank_fields", [])
    customers, customer_note = _load_customers(client, manifest, detail_config, cache)
    customers = {identity: {**metadata, **detail_config.get("customer_overrides", {}).get(
        metadata.get("customer_code", ""), {})} for identity, metadata in customers.items()}
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


def run(cache: CatalogueCache | None = None) -> dict[str, Any]:
    TODO_UPDATES.clear()
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
        client = MobiWorkClient.from_env() if cache is None else cache.client
        extra_uploads: list[tuple[Path, str]] = []
        primary_folder = cfg.folder
        if source == "calc":
            path, primary_folder, extra_uploads = _build_calc_workbook(client, cfg, manifest, dry_run, cache)
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
                    # A workbook someone has open in Excel is locked (HTTP 423); the copy is
                    # retried next run. Any other failure of the current snapshot is fatal,
                    # and a locked current snapshot is fatal unless the monthly copy publishes.
                    locked = "423" in str(exc) or "locked" in str(exc).casefold()
                    if required and not locked:
                        raise
                    if required:
                        manifest["current_snapshot_locked"] = True
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
            if manifest.get("current_snapshot_locked") and os.environ.get("GITHUB_ACTIONS") == "true":
                print("::warning title=BaoCaoTraThuong_Current đang mở::File Current đang được mở trong Excel nên "
                      "chưa ghi đè; file tháng đã cập nhật. Đóng file để lần chạy sau cập nhật Current.")
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
                                                "" if dry_run else drive_id, dry_run=dry_run,
                                                allow_blank_fields=manifest.get("allow_blank_fields", []))
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
