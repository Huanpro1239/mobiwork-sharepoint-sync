"""Read-only, PII-free evidence for the documented PromotionBonus APIs."""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mobiwork import MobiWorkClient
from promotion_catalogue import fetch_programs, load_config

TOTAL_PRICE_QUANTITY = "total_price_quantity"
UI_REPORT_URL = "https://dms.mobiwork.vn:3020/PromotionBonusReport"


def ui_report_request(orgid: str, programs: list[str], start: str, end: str, sttt: str = ""):
    """Build the captured Chrome contract; no inferred auth or calculation enum.

    End date is the observed local midnight, not a guessed inclusive end-of-day.
    This builder does not enable production export or execute a network request.
    """
    first, last = date_range(start, end)
    if not re.fullmatch(r"[a-fA-F0-9]{24}", orgid or ""):
        raise ValueError("UI request requires an organization ID")
    if not isinstance(programs, list) or not programs:
        raise ValueError("UI request requires explicit selected programs")
    if any(not isinstance(p, str) or not re.fullmatch(r"[a-fA-F0-9]{24}", p) for p in programs):
        raise ValueError("Invalid UI program ID")
    if len(set(programs)) != len(programs):
        raise ValueError("Duplicate selected program IDs")
    zone = ZoneInfo("Asia/Ho_Chi_Minh")
    def epoch(d):
        return int(datetime(d.year, d.month, d.day, tzinfo=zone).timestamp() * 1000)
    request = {"method": "POST", "url": UI_REPORT_URL,
            "params": {"orgid": orgid, "projectID": "", "projectName": "",
                       "assignTo": "", "eeName": "", "idcustomer": "",
                       "startDate": epoch(first), "endDate": epoch(last)},
            "json": {"arrCT": list(programs)}}
    if not isinstance(sttt, str):
        raise ValueError("Observed sttt must be a string; no calculation enum inferred")
    if sttt:
        request["params"]["sttt"] = sttt
    return request


def date_range(start: str = "", end: str = "", today: date | None = None):
    today = today or datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
    first = date.fromisoformat(start) if start else today.replace(day=1)
    last = date.fromisoformat(end) if end else today.replace(day=calendar.monthrange(today.year, today.month)[1])
    if first > last:
        raise ValueError("from_date must not be after to_date")
    return first, last


DATE_VARIANTS = ("id_only", "epoch_ui", "epoch_full_day", "ddmmyyyy")


def _local_epoch_ms(d: date, end_of_day: bool = False) -> int:
    zone = ZoneInfo("Asia/Ho_Chi_Minh")
    moment = datetime(d.year, d.month, d.day, tzinfo=zone)
    millis = int(moment.timestamp() * 1000)
    return millis + 86_399_999 if end_of_day else millis


def report_date_params(variant: str, first: date, last: date) -> dict:
    """Candidate report date bindings. Each is a probe, never assumed correct.

    epoch_ui       -> exactly what the Paybonus page sends (local midnight epoch ms).
    epoch_full_day -> endDate at 23:59:59.999, as the captured backend query expands it.
    ddmmyyyy       -> fromdate/todate as documented for the PromotionBonus catalogue.
    """
    if variant == "id_only":
        return {}
    if variant == "epoch_ui":
        return {"startDate": _local_epoch_ms(first), "endDate": _local_epoch_ms(last)}
    if variant == "epoch_full_day":
        return {"startDate": _local_epoch_ms(first), "endDate": _local_epoch_ms(last, True)}
    if variant == "ddmmyyyy":
        return {"fromdate": first.strftime("%d/%m/%Y"), "todate": last.strftime("%d/%m/%Y")}
    raise ValueError(f"Unknown report date variant: {variant!r}")


def parse_variants(raw) -> tuple[str, ...]:
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    variants = tuple(dict.fromkeys(v.strip() for v in items if v and v.strip()))
    if not variants:
        raise ValueError("At least one report date variant is required")
    unknown = [v for v in variants if v not in DATE_VARIANTS]
    if unknown:
        raise ValueError(f"Unknown report date variant(s): {', '.join(unknown)}")
    return variants


def request_parameters(program_id: str, sttt: str = ""):
    if not program_id or "," in program_id or any(c.isspace() for c in program_id):
        raise ValueError("Use exactly one catalogue program ID")
    params = {"id_ct": program_id}
    if sttt:
        params["sttt"] = sttt
    return params


def field_paths(value, prefix="", depth=0):
    # Only schema keys, never customer values, arbitrary identifiers or contact keys.
    if depth > 4:
        return set()
    found = set()
    if isinstance(value, list):
        for item in value[:20]:
            found.update(field_paths(item, prefix, depth + 1))
    elif isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", key):
                continue
            if re.fullmatch(r"[a-fA-F0-9]{24}", key):
                continue
            path = f"{prefix}.{key}" if prefix else key
            found.add(path)
            found.update(field_paths(child, path, depth + 1))
    return found


def run_audit(client, cfg, start="", end="", program="all", sttt="",
              calculation_mode=TOTAL_PRICE_QUANTITY, date_variants=("id_only",)):
    variants = parse_variants(date_variants)
    if calculation_mode != TOTAL_PRICE_QUANTITY:
        raise ValueError("Unsupported semantic calculation mode; no API enum verified")
    first, last = date_range(start, end)
    # These date parameters belong to PromotionBonus catalogue, NOT the report.
    filters = {"fromdate": first.strftime("%d/%m/%Y"), "todate": last.strftime("%d/%m/%Y")}
    programs = fetch_programs(client, cfg, filters=filters)
    if program != "all":
        programs = [p for p in programs if str(p.get("_id")) == program]
        if not programs:
            raise ValueError("Selected program not returned by dated catalogue")
    audit = {"status": "evidence_incomplete", "from_date": first.isoformat(),
             "to_date": last.isoformat(), "calculation_mode": calculation_mode,
             "report_date_binding_verified": False, "calculation_binding_verified": False,
             "catalog_parameter_names": ["page_size", "page_number", *filters],
             "catalog_field_paths": sorted(field_paths(programs)),
             "program_count": len(programs), "reports": [],
             "missing_evidence": ["report date parameter names and formats",
                                  "sttt value for total price multiplied by quantity",
                                  "customer/region/target/reward response schema",
                                  "dated catalogue selection equivalence to DMS all"]}
    audit["date_variants"] = list(variants)
    number = 0
    for item in programs:
        base = request_parameters(str(item["_id"]), sttt)
        for variant in variants:
            number += 1
            params = {**base, **report_date_params(variant, first, last)}
            payload = client.get_json(cfg.report_url, params,
                                      operation_key="promotion_api_audit", request_number=number)
            entry = {"date_variant": variant,
                     "request_parameter_names": sorted(params),
                     "sttt_source": "explicit_caller_probe" if sttt else "omitted",
                     "response_fields": sorted(field_paths(payload)),
                     "status": payload.get("status") is True,
                     "total": payload.get("total") if type(payload.get("total")) is int else None}
            for key in ("data", "arrChiTieu", "arrTraThuong"):
                rows = payload.get(key)
                entry[key] = {"count": len(rows) if isinstance(rows, list) else None,
                              "fields": sorted(field_paths(rows))}
            audit["reports"].append(entry)
    by_variant = {v: 0 for v in variants}
    programs_with_rows = {v: 0 for v in variants}
    for report in audit["reports"]:
        rows = report["data"]["count"] or 0
        by_variant[report["date_variant"]] += rows
        programs_with_rows[report["date_variant"]] += 1 if rows else 0
    audit["customer_rows_by_variant"] = by_variant
    audit["programs_with_rows_by_variant"] = programs_with_rows
    audit["variants_with_rows"] = [v for v in variants if by_variant[v] > 0]
    best = max(variants, key=lambda v: by_variant[v])
    audit["best_variant"] = best if by_variant[best] > 0 else None
    audit["customer_rows"] = by_variant[best]
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", default=os.getenv("PROMOTION_BONUS_FROM_DATE", ""))
    parser.add_argument("--to-date", default=os.getenv("PROMOTION_BONUS_TO_DATE", ""))
    parser.add_argument("--program", default=os.getenv("PROMOTION_BONUS_PROGRAM", "all"))
    parser.add_argument("--calculation-mode", default=TOTAL_PRICE_QUANTITY)
    parser.add_argument("--sttt", default=os.getenv("PROMOTION_BONUS_STTT", ""),
                        help="Explicit raw probe value; no calculation meaning is inferred")
    parser.add_argument("--date-variants",
                        default=os.getenv("PROMOTION_BONUS_DATE_VARIANTS") or ",".join(DATE_VARIANTS),
                        help="Comma list of report date probes: " + ", ".join(DATE_VARIANTS))
    parser.add_argument("--golden", type=Path, help="Local DMS workbook; never upload it")
    parser.add_argument("--ui-response", type=Path, help="Inspect a local Chrome JSON response without API credentials")
    args = parser.parse_args(argv)
    try:
        if args.ui_response:
            payload = json.loads(args.ui_response.read_text(encoding="utf-8-sig"))
            audit = ui_response_summary(payload)
        else:
            audit = run_audit(MobiWorkClient.from_env(), load_config(), args.from_date,
                              args.to_date, args.program, args.sttt, args.calculation_mode,
                              args.date_variants)
        if args.golden:
            audit["golden_reference"] = golden_summary(args.golden)
            golden_rows = audit["golden_reference"]["customer_rows"]
            audit["row_count_matches_golden"] = audit["customer_rows"] == golden_rows
            if "customer_rows_by_variant" in audit:
                audit["golden_match_by_variant"] = {
                    v: n == golden_rows for v, n in audit["customer_rows_by_variant"].items()}
    except Exception as exc:
        audit = {"status": "failed", "error_type": type(exc).__name__}
        _save(audit)
        raise SystemExit("API diagnostic failed; see sanitized error_type") from None
    _save(audit)
    print(f"Diagnostic status: {audit['status']}; see sanitized metadata in output/promotion_api_audit.json")
    if "customer_rows_by_variant" in audit:
        for variant, rows in audit["customer_rows_by_variant"].items():
            hits = audit["programs_with_rows_by_variant"][variant]
            print(f"  date_variant={variant}: customer_rows={rows}, programs_with_rows={hits}")
        print(f"  best_variant={audit['best_variant']}")
        _github_notice(audit)


def _github_notice(audit):
    """Expose sanitized per-variant counts as run annotations (no customer values)."""
    if os.getenv("GITHUB_ACTIONS") != "true":
        return
    counts = ", ".join(
        f"{v}={n} rows/{audit['programs_with_rows_by_variant'][v]} programs"
        for v, n in audit["customer_rows_by_variant"].items())
    print(f"::notice title=PromotionBonusReport date probe::{counts}; "
          f"best_variant={audit['best_variant']}; programs={audit['program_count']}")


def _save(audit):
    path = Path("output/promotion_api_audit.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(audit, indent=2), encoding="utf-8")


def ui_response_summary(payload):
    """Preserve envelope multiplicity; raw order results are not customer rewards."""
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
        raise ValueError("UI response requires a result array")
    envelopes = payload["result"]
    if any(not isinstance(e, dict) or not isinstance(e.get("result"), list) for e in envelopes):
        raise ValueError("Only observed nested-envelope schema is supported")
    rows = [r for envelope in envelopes for r in envelope["result"]]
    if any(not isinstance(r, dict) for r in rows):
        raise ValueError("Nested records must be objects")
    dates = []
    for envelope in envelopes:
        query = envelope.get("options", {}).get("$query", {})
        span = query.get("data.ngay_giao_hang.viewData", {})
        if type(span.get("$gte")) is int and type(span.get("$lte")) is int:
            dates.append({"from_epoch_ms": span["$gte"], "to_epoch_ms": span["$lte"]})
    return {"status": "evidence_incomplete", "schema": "nested_record_envelopes",
            "envelope_count": len(envelopes),
            "nonempty_envelopes": sum(bool(e["result"]) for e in envelopes),
            "nested_record_count": len(rows), "customer_rows": None,
            "all_envelopes_are_orders": bool(envelopes) and all(e.get("RecordType") == "_Orders" for e in envelopes),
            "record_field_paths": sorted(field_paths(rows)),
            "delivery_date_filters": dates,
            "target_count": len(payload.get("arrChiTieu") or []),
            "reward_count": len(payload.get("arrTraThuong") or []),
            "missing_evidence": ["final customer/program/level report rows", "target and reward definitions",
                                 "authenticated UI-service access"]}


def golden_summary(path):
    """Counts only. DMS exports can incorrectly declare every sheet as A1:A1."""
    from collections import Counter
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    regions = Counter()
    sheets = 0
    try:
        for sheet in workbook:
            sheet.reset_dimensions()
            header = None
            for row in sheet.iter_rows(values_only=True):
                if "Mã khách hàng" in row and "Khu vực" in row:
                    header = {str(value): index for index, value in enumerate(row) if value}
                    sheets += 1
                    continue
                if header and row and type(row[0]) in (int, float):
                    code = header["Mã khách hàng"]
                    region = header["Khu vực"]
                    if len(row) > max(code, region) and row[code]:
                        name = str(row[region] or "Chưa xác định").strip()
                        regions[name] += 1
    finally:
        workbook.close()
    return {"customer_rows": sum(regions.values()), "region_distribution": dict(regions),
            "report_sheets": sheets}


if __name__ == "__main__":
    main()
