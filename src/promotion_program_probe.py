"""Read-only probe: is a programme in the DMS catalogue, which months return it, can it be
computed, and is there order data for its whole accumulation period?

Usage (GitHub Actions, report_scope=promotion_api_audit, bonus_program="246/TB,248/TB"):
prints ``::notice`` lines with counts and rule shapes only (no customer data).
"""
from __future__ import annotations

import collections
import json
import os
from datetime import date, datetime, timedelta
from typing import Any

from promotion_bonus_ui import VN_TZ

BILL_URL = "https://openapi.mobiwork.vn/OpenAPI/V1/Bill"


def notice(title: str, payload: Any) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    print(f"::notice title={title}::{' '.join(text.split())[:3800]}")


def vn_day(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, VN_TZ).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def label(value: Any) -> Any:
    return value.get("label") or value.get("value") if isinstance(value, dict) else value


def month_starts(first: date, last: date) -> list[date]:
    months, cursor = [], first.replace(day=1)
    while cursor <= last:
        months.append(cursor)
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return months


def month_end(first: date) -> date:
    return (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def shape(program: dict[str, Any]) -> dict[str, Any]:
    import promotion_bonus_calc as calc

    products = program.get("products") or []
    rule = products[0] if products and isinstance(products[0], dict) else {}
    buy = rule.get("san_pham_mua") or []
    gifts = [g for group in rule.get("san_pham_khuyen_mai") or [] for g in (group if isinstance(group, list) else [group])]
    out: dict[str, Any] = {
        "id": program.get("_id"), "name": program.get("name"),
        "period": [vn_day(program.get("startDate")), vn_day(program.get("endDate"))],
        "register": [vn_day(program.get("registerStartDate")), vn_day(program.get("registerEndDate"))],
        "ptype": label(program.get("ptype")), "ptype_value": (program.get("ptype") or {}).get("value")
        if isinstance(program.get("ptype"), dict) else None,
        "ctype": label(program.get("ctype")), "gtype": label(program.get("gtype")), "ktype": label(program.get("ktype")),
        "settings": program.get("settings"), "soSuat": program.get("soSuat"),
        "deleted": program.get("isDeleted"), "archived": program.get("isArchived"),
        "customers": len(program.get("customer") or []), "product_rules": len(products),
        "rule_keys": sorted(rule)[:20], "yeu_cau": rule.get("yeu_cau"),
        "buy": [f"{b.get('ma_san_pham')}|{label(b.get('don_vi_tinh'))}" for b in buy][:12],
        "gifts": [f"{g.get('ma_san_pham')}|{g.get('ten_san_pham')}|{label(g.get('don_vi_tinh'))}|{g.get('so_luong')}"
                  for g in gifts if isinstance(g, dict)][:6],
        "all_products": (rule.get("chon_tat_ca_sp") or {}).get("chon_tat_ca_sp")
        if isinstance(rule.get("chon_tat_ca_sp"), dict) else rule.get("chon_tat_ca_sp"),
    }
    try:
        parsed = calc.parse_rule(program)
        out["parsed"] = {"kind": parsed.kind, "min": parsed.minimum, "max": parsed.maximum,
                         "rewards": len(parsed.rewards), "multiple": parsed.multiple}
    except Exception as exc:  # the reason it is skipped by the calculator
        out["parse_error"] = f"{type(exc).__name__}: {exc}"
    return out


def bill_total(client: Any, first: date, last: date) -> Any:
    try:
        payload = client.get_json(BILL_URL, {"tu_ngay": first.strftime("%d/%m/%Y"),
                                             "den_ngay": last.strftime("%d/%m/%Y"),
                                             "kieu_ngay": "cdate", "trang_thai": "",
                                             "page_size": 1, "page_number": 1},
                                  operation_key="probe_bill", request_number=1)
        return payload.get("total")
    except Exception as exc:
        return f"{type(exc).__name__}"


def run() -> None:
    from mobiwork import MobiWorkClient
    from promotion_bonus import fetch_programs, load_config

    codes = [c.strip().casefold() for c in os.environ.get("PROMOTION_PROBE_CODES", "").split(",") if c.strip()]
    if not codes:
        print("::warning title=Programme probe::no codes")
        return
    client, cfg = MobiWorkClient.from_env(), load_config()
    catalogue = fetch_programs(client, cfg)
    matches = [p for p in catalogue if any(c in str(p.get("name", "")).casefold() for c in codes)]
    notice("Probe catalogue", {"catalogue": len(catalogue), "matches": len(matches),
                               "names": [str(p.get("name"))[:80] for p in matches]})
    for program in matches[:6]:
        notice(f"Probe {str(program.get('name'))[:40]}", shape(program))
    if not matches:
        return
    first = min(datetime.fromtimestamp(int(p["startDate"]) / 1000, VN_TZ).date() for p in matches if p.get("startDate"))
    last = max(datetime.fromtimestamp(int(p["endDate"]) / 1000, VN_TZ).date() for p in matches if p.get("endDate"))
    months = month_starts(first, min(last, datetime.now(VN_TZ).date()))
    listed: dict[str, list[str]] = collections.defaultdict(list)
    bills: dict[str, Any] = {}
    ids = {str(p.get("_id")) for p in matches}
    for month in months:
        filters = {"fromdate": month.strftime("%d/%m/%Y"), "todate": month_end(month).strftime("%d/%m/%Y")}
        try:
            found = {str(p.get("_id")) for p in fetch_programs(client, cfg, filters=filters)} & ids
        except Exception as exc:
            found = {f"error {type(exc).__name__}"}
        listed[f"{month:%Y-%m}"] = sorted(found)
        bills[f"{month:%Y-%m}"] = bill_total(client, month, month_end(month))
    notice("Probe months", {"programme_ids_returned_by_month_filter": listed,
                            "bill_orders_in_DMS_by_month(cdate)": bills})
    sales_probe(matches, months)


def sales_probe(programs: list[dict[str, Any]], months: list[date]) -> None:
    """Accumulated sales of registered customers over the available monthly masters."""
    import promotion_bonus_calc as calc
    from promotion_bonus import _bill_detail

    rules = []
    for program in programs:
        try:
            rules.append(calc.parse_rule(program))
        except Exception:
            continue
    totals: dict[str, dict[str, float]] = {r.program_id: collections.defaultdict(float) for r in rules}
    available, missing = [], []
    first_day = min(datetime.fromtimestamp(int(p["startDate"]) / 1000, VN_TZ).date() for p in programs)
    last_day = max(datetime.fromtimestamp(int(p["endDate"]) / 1000, VN_TZ).date() for p in programs)
    for month in months:
        try:
            detail = _bill_detail(month, False, {})
        except Exception as exc:
            missing.append(f"{month:%Y-%m} {type(exc).__name__}")
            continue
        available.append(f"{month:%Y-%m}")
        lines = calc.sold_lines(detail, max(month, first_day), min(month_end(month), last_day))
        for rule in rules:
            for line in lines:
                if line["customer"] in rule.customers and line["unit"] in rule.units.get(line["sku"], ()):
                    totals[rule.program_id][line["customer"]] += (
                        line["amount"] if rule.kind == calc.AMOUNT else line["quantity"])
    result = {"masters_available": available, "masters_missing": missing, "programmes": []}
    for rule in rules:
        values = totals[rule.program_id]
        result["programmes"].append({"name": rule.name[:60], "registered": len(rule.customers),
                                     "with_sales": len(values), "reached": sum(calc.reached(v, rule) for v in values.values()),
                                     "min": rule.minimum, "max": rule.maximum,
                                     "top_actual": sorted(values.values(), reverse=True)[:3]})
    notice("Probe accumulated sales", result)


if __name__ == "__main__":
    run()
