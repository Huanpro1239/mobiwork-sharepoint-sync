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
from promotion_bonus import fetch_programs, load_config

TOTAL_PRICE_QUANTITY = "total_price_quantity"


def date_range(start: str = "", end: str = "", today: date | None = None):
    today = today or datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
    first = date.fromisoformat(start) if start else today.replace(day=1)
    last = date.fromisoformat(end) if end else today.replace(day=calendar.monthrange(today.year, today.month)[1])
    if first > last:
        raise ValueError("from_date must not be after to_date")
    return first, last


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
              calculation_mode=TOTAL_PRICE_QUANTITY):
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
             "program_count": len(programs), "reports": [],
             "missing_evidence": ["report date parameter names and formats",
                                  "sttt value for total price multiplied by quantity",
                                  "customer/region/target/reward response schema",
                                  "dated catalogue selection equivalence to DMS all"]}
    for number, item in enumerate(programs, 1):
        params = request_parameters(str(item["_id"]), sttt)
        payload = client.get_json(cfg.report_url, params,
                                  operation_key="promotion_api_audit", request_number=number)
        entry = {"request_parameter_names": sorted(params),
                 "sttt_source": "explicit_caller_probe" if sttt else "omitted",
                 "response_fields": sorted(field_paths(payload)),
                 "status": payload.get("status") is True,
                 "total": payload.get("total") if type(payload.get("total")) is int else None}
        for key in ("data", "arrChiTieu", "arrTraThuong"):
            rows = payload.get(key)
            entry[key] = {"count": len(rows) if isinstance(rows, list) else None,
                          "fields": sorted(field_paths(rows))}
        audit["reports"].append(entry)
    audit["customer_rows"] = sum(r["data"]["count"] or 0 for r in audit["reports"])
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", default=os.getenv("PROMOTION_BONUS_FROM_DATE", ""))
    parser.add_argument("--to-date", default=os.getenv("PROMOTION_BONUS_TO_DATE", ""))
    parser.add_argument("--program", default=os.getenv("PROMOTION_BONUS_PROGRAM", "all"))
    parser.add_argument("--calculation-mode", default=TOTAL_PRICE_QUANTITY)
    parser.add_argument("--sttt", default=os.getenv("PROMOTION_BONUS_STTT", ""),
                        help="Explicit raw probe value; no calculation meaning is inferred")
    parser.add_argument("--golden", type=Path, help="Local DMS workbook; never upload it")
    args = parser.parse_args(argv)
    try:
        audit = run_audit(MobiWorkClient.from_env(), load_config(), args.from_date,
                          args.to_date, args.program, args.sttt, args.calculation_mode)
        if args.golden:
            audit["golden_reference"] = golden_summary(args.golden)
            audit["row_count_matches_golden"] = audit["customer_rows"] == audit["golden_reference"]["customer_rows"]
    except Exception as exc:
        audit = {"status": "failed", "error_type": type(exc).__name__}
        _save(audit)
        raise SystemExit("API diagnostic failed; see sanitized error_type") from None
    _save(audit)
    print(f"API diagnostic: {audit['program_count']} programs, {audit['customer_rows']} customer rows; evidence incomplete")


def _save(audit):
    path = Path("output/promotion_api_audit.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(audit, indent=2), encoding="utf-8")


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
