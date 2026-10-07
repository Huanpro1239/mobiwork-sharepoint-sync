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


def chains(sales: list[dict[str, Any]], groups: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    by_code = {str(g.get("ma_nhom") or "").strip(): g for g in groups if g.get("ma_nhom")}
    out: dict[str, list[dict[str, str]]] = {}
    for sale in sales:
        code = str(sale.get("ma") or "").strip()
        node = str(sale.get("ma_don_vi") or "").strip()
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


def supervisor_probe(sales: list[dict[str, Any]], sample: str = "KHA04") -> dict[str, Any]:
    """Shape of role fields, to find how supervisors (SS) are represented."""
    pick = lambda s: {k: str(s.get(k) or "") for k in ("ma", "chuc_vu", "chuc_danh", "bo_phan", "ma_don_vi")}  # noqa: E731
    short = [s for s in sales if re.fullmatch(r"[A-Z]{2,4}\d{2}", str(s.get("ma") or "").strip())]
    return {"chuc_vu": dict(collections.Counter(str(s.get("chuc_vu") or "") for s in sales).most_common(12)),
            "chuc_danh": dict(collections.Counter(str(s.get("chuc_danh") or "") for s in sales).most_common(12)),
            "bo_phan": dict(collections.Counter(str(s.get("bo_phan") or "") for s in sales).most_common(12)),
            "short_codes": len(short),
            "short_units": dict(collections.Counter(str(s.get("ma_don_vi") or "") for s in short).most_common(12)),
            "sample": [pick(s) for s in sales if str(s.get("ma") or "").strip() == sample],
            "sample_employee": [pick(s) for s in sales if str(s.get("ma") or "").strip() == "KHHO0303"]}


def main() -> None:
    from mobiwork import MobiWorkClient

    sales, groups = fetch(MobiWorkClient.from_env())
    result = summary(sales, groups, os.environ.get("SALES_STRUCTURE_SAMPLE", "KHHO0303"))
    print(f"::notice title=Sales structure::{json.dumps(result, ensure_ascii=False)[:3500]}")
    print(f"::notice title=Sales roles::{json.dumps(supervisor_probe(sales), ensure_ascii=False)[:3500]}")


if __name__ == "__main__":
    main()
