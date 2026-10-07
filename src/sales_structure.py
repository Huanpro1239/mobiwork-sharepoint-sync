"""Derive Vùng / SS / NPP for each sales employee from the DMS department tree.

OpenAPI ``Sale`` gives each employee's unit (``ma_don_vi``) and ``SaleGroup`` gives the
department tree (``ma_nhom``, ``ten_nhom``, ``ma_nhom_cha``, ``loai_nhom``). Group names
follow "<code> - <name>" (e.g. "B-KHHO-0288 - TU TAI" for a distributor).
"""
from __future__ import annotations

import collections
import json
import os
import re
from typing import Any

SALE_URL = "https://openapi.mobiwork.vn/OpenAPI/V1/Sale"
GROUP_URL = "https://openapi.mobiwork.vn/OpenAPI/V1/SaleGroup"
CODE_NAME = re.compile(r"^\s*(?P<code>\S+)\s+-\s+(?P<name>.+?)\s*$")


def _rows(payload: Any, label: str) -> list[dict[str, Any]]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{label} response is missing data array")
    return [row for row in rows if isinstance(row, dict)]


def fetch(client: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sales = _rows(client.get_json(SALE_URL, {}, operation_key="sales", request_number=1), "Sale")
    groups = _rows(client.get_json(GROUP_URL, {}, operation_key="sale_groups", request_number=1), "SaleGroup")
    return sales, groups


def units(sale: dict[str, Any]) -> list[str]:
    """A user can belong to several units, stored comma-separated in ``ma_don_vi``."""
    return [u.strip() for u in str(sale.get("ma_don_vi") or "").split(",") if u.strip()]


def chains(sales: list[dict[str, Any]], groups: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    by_code = {str(g.get("ma_nhom") or "").strip(): g for g in groups if g.get("ma_nhom")}
    out: dict[str, list[dict[str, str]]] = {}
    for sale in sales:
        code = str(sale.get("ma") or "").strip()
        node = (units(sale) or [""])[0]
        chain, seen = [], set()
        while node and node in by_code and node not in seen:
            seen.add(node)
            group = by_code[node]
            chain.append({"ma_nhom": node, "ten_nhom": str(group.get("ten_nhom") or "").strip(),
                          "loai_nhom": str(group.get("loai_nhom") or "").strip()})
            node = str(group.get("ma_nhom_cha") or "").strip()
        if code:
            out[code] = chain
    return out


def summary(sales: list[dict[str, Any]], groups: list[dict[str, Any]], sample: str = "") -> dict[str, Any]:
    tree = chains(sales, groups)
    depth = collections.Counter(len(c) for c in tree.values())
    kinds = collections.Counter(str(g.get("loai_nhom") or "") for g in groups)
    patterned = sum(1 for g in groups if CODE_NAME.match(str(g.get("ten_nhom") or "")))
    return {"sales": len(sales), "groups": len(groups), "chain_depths": dict(depth),
            "group_kinds": dict(kinds.most_common(10)), "groups_code_dash_name": patterned,
            "sale_fields": sorted({k for s in sales for k in s})[:20],
            "group_fields": sorted({k for g in groups for k in g})[:10],
            "sample_employee": sample, "sample_chain": tree.get(sample, [])}


def main() -> None:
    from mobiwork import MobiWorkClient

    sales, groups = fetch(MobiWorkClient.from_env())
    cfg = employee_mapping(sales, groups)
    result = summary(sales, groups, os.environ.get("SALES_STRUCTURE_SAMPLE", ""))
    result.update({"derived_employees": len(cfg),
                   "with_ss": sum(bool(v.get("SS Code")) for v in cfg.values()),
                   "with_npp": sum(bool(v.get("DB Code")) for v in cfg.values()),
                   "with_region": sum(bool(v.get("Vùng")) for v in cfg.values())})
    print(f"::notice title=Sales structure::{json.dumps(result, ensure_ascii=False)[:3500]}")


NPP_UNIT = re.compile(r"^[A-Z]-[A-Z]+-\d+$")
REGION = re.compile(r"^(MB|MT|MN|TN)(.*)$")
REGION_NAMES = {"MB": "Miền Bắc", "MT": "Miền Trung", "MN": "Miền Nam", "TN": "Tây Nguyên"}
SUPERVISOR = "giám sát kinh doanh"


def is_supervisor(sale: dict[str, Any]) -> bool:
    """Chức vụ "Giám sát kinh doanh", or chức danh "Giám sát" when chức vụ is blank."""
    norm = lambda v: " ".join(str(v or "").split()).casefold()  # noqa: E731
    role, title = norm(sale.get("chuc_vu")), norm(sale.get("chuc_danh"))
    return SUPERVISOR in role or (not role and title == "giám sát")


def region_label(code: str) -> str:
    match = REGION.match(code.strip().upper())
    if not match:
        return ""
    rest = match.group(2).strip()
    return f"{REGION_NAMES[match.group(1)]} {rest}".strip()


def _supervisor_index(sales: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]],
                                                              dict[str, list[dict[str, Any]]]]:
    """Supervisors per unit, and per province (B-<PROV>-NNNN units only)."""
    supervisors: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for sale in sales:
        if is_supervisor(sale):
            for unit in units(sale):
                supervisors[unit].append(sale)
    province_supervisors: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for unit_code, people in supervisors.items():
        if NPP_UNIT.match(unit_code):
            for person in people:
                bucket = province_supervisors[unit_code.split("-")[1]]
                if person not in bucket:
                    bucket.append(person)
    return supervisors, province_supervisors


def _person(sale: dict[str, Any]) -> tuple[str, str]:
    return str(sale.get("ma") or "").strip(), " ".join(str(sale.get("ten") or "").split())


def _assign(item: dict[str, str], chain: list[dict[str, str]], supervisors: dict[str, list[dict[str, Any]]],
            province_supervisors: dict[str, list[dict[str, Any]]], exclude: str = "") -> None:
    """Fill SS (own unit, else nearest ancestor, else unique province supervisor) and Vùng."""
    for node in chain:
        found = sorted(supervisors.get(node["ma_nhom"], []), key=lambda s: str(s.get("ma") or ""))
        found = [s for s in found if _person(s)[0] != exclude]
        if found:
            item["SS Code"] = "; ".join(_person(s)[0] for s in found)
            item["SS Name"] = "; ".join(_person(s)[1] for s in found)
            break
    unit = chain[0]["ma_nhom"] if chain else ""
    if "SS Code" not in item and NPP_UNIT.match(unit):
        # Unit without its own supervisor: use the province's supervisor only when the
        # province (B-<PROV>-NNNN) has exactly one; several -> leave blank for review.
        only = province_supervisors.get(unit.split("-")[1], [])
        if len(only) == 1 and _person(only[0])[0] != exclude:
            item["SS Code"], item["SS Name"] = _person(only[0])
    for node in chain[1:] or chain:
        label = region_label(node["ma_nhom"])
        if label:
            item["Vùng"] = label
            break


def _unit_chain(code: str, by_code: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    chain, seen, node = [], set(), code
    while node and node in by_code and node not in seen:
        seen.add(node)
        group = by_code[node]
        chain.append({"ma_nhom": node, "ten_nhom": str(group.get("ten_nhom") or "").strip(),
                      "loai_nhom": str(group.get("loai_nhom") or "").strip()})
        node = str(group.get("ma_nhom_cha") or "").strip()
    return chain


def employee_mapping(sales: list[dict[str, Any]], groups: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Mã NV -> Vùng / SS Code / SS Name / DB Code / Tên NPP from the department tree.

    * DB Code / Tên NPP: the employee's own unit when it is a distributor unit (B-XXXX-NNNN).
    * SS: the "Giám sát kinh doanh" employee(s) of the same unit; when the unit has none,
      the nearest ancestor unit with supervisors. Several supervisors are joined with "; ".
    * Vùng: the nearest ancestor coded MB/MT/MN/TN (e.g. MT1B -> "Miền Trung 1B").
    """
    tree = chains(sales, groups)
    supervisors, province_supervisors = _supervisor_index(sales)
    out: dict[str, dict[str, str]] = {}
    for sale in sales:
        code = str(sale.get("ma") or "").strip()
        chain = tree.get(code) or []
        if not code or not chain:
            continue
        item: dict[str, str] = {}
        unit = chain[0]
        if NPP_UNIT.match(unit["ma_nhom"]):
            item["DB Code"] = unit["ma_nhom"]
            item["Tên NPP"] = unit["ten_nhom"]
        _assign(item, chain, supervisors, province_supervisors, exclude=code)
        if item:
            out[code] = item
    return out


def unit_mapping(sales: list[dict[str, Any]], groups: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """NPP unit (B-XXXX-NNNN, the order's warehouse code) -> Tên NPP / SS / Vùng."""
    by_code = {str(g.get("ma_nhom") or "").strip(): g for g in groups if g.get("ma_nhom")}
    supervisors, province_supervisors = _supervisor_index(sales)
    out: dict[str, dict[str, str]] = {}
    for code in by_code:
        if not NPP_UNIT.match(code):
            continue
        chain = _unit_chain(code, by_code)
        item = {"DB Code": code, "Tên NPP": chain[0]["ten_nhom"]}
        _assign(item, chain, supervisors, province_supervisors)
        out[code] = item
    return out


def supervisor_candidates(sales: list[dict[str, Any]]) -> dict[str, str]:
    """Province code -> "MA - Tên; ..." of its supervisors, as a hint for manual filling."""
    _, province_supervisors = _supervisor_index(sales)
    return {province: "; ".join(" - ".join(_person(s)) for s in people)
            for province, people in province_supervisors.items()}


def enrich_employee_config(client: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """Add derived employee mappings; explicit config entries always win."""
    sales, groups = fetch(client)
    derived = employee_mapping(sales, groups)
    result = dict(cfg)
    employees = {code: dict(values) for code, values in (cfg.get("employees") or {}).items()}
    for code, values in derived.items():
        merged = dict(values)
        merged.update(employees.get(code, {}))
        employees[code] = merged
    result["employees"] = employees
    # Explicit mappings stay distinguishable from derived ones: the order's warehouse
    # (historical NPP) beats the current department tree, but never an explicit entry.
    result["employees_explicit"] = {code: dict(values) for code, values in (cfg.get("employees") or {}).items()}
    npp_units = unit_mapping(sales, groups)
    for code, values in (cfg.get("npp_units") or {}).items():
        npp_units[code] = {**npp_units.get(code, {}), **values}
    result["npp_units"] = npp_units
    result["supervisor_candidates"] = supervisor_candidates(sales)
    result["sales_structure_audit"] = {"sales": len(sales), "groups": len(groups),
                                       "derived_employees": len(derived),
                                       "with_ss": sum(1 for v in derived.values() if v.get("SS Code")),
                                       "with_npp": sum(1 for v in derived.values() if v.get("DB Code")),
                                       "npp_units": len(npp_units),
                                       "npp_units_with_ss": sum(1 for v in npp_units.values() if v.get("SS Code"))}
    return result


if __name__ == "__main__":
    main()
