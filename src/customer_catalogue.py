from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mobiwork import MobiWorkClient
from promotion_bonus import _api_total, _expect_object_list

FIELDS = {"Tỉnh": "tinh_thanh_moi", "Loại KH": "loai_kh", "Route": "code_router",
          "Tên Khách hàng": "tenkh", "Địa chỉ": "dia_chi", "Số ĐT": "sdt",
          "Nhóm KH": "nhom_kh"}


def value(row: dict[str, Any], field: str) -> str:
    raw = row.get(field)
    if raw is None:
        return ""
    if isinstance(raw, (dict, list, bool)):
        raise ValueError(f"Customer catalogue {field} must be scalar text")
    return str(raw).strip()


RETRYABLE = ("Customer catalogue total changed", "Customer catalogue total mismatch",
             "Customer catalogue repeated page")


def enrich_customer_config(client: MobiWorkClient, cfg: dict[str, Any], attempts: int = 3) -> dict[str, Any]:
    """Join current customer metadata by the observed API ID, never by name or code alone.

    Customers are created all day long, so one paginated pass up to "today" can see the
    total change between pages. The catalogue is therefore read in two creation-date
    windows: everything up to yesterday (stable) and today (small, cheap to re-read).
    Each window is retried independently when its total changes while paging.
    """
    start = cfg.get("customer_catalogue_start_date", "01/01/1900")
    first = datetime.strptime(start, "%d/%m/%Y").date()
    today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
    windows = [(first, today)] if first >= today else [(first, today - timedelta(days=1)), (today, today)]
    customers: dict[str, Any] = {}
    audit = {"source_rows": 0, "unkeyed_rows": 0, "fields": set(), "structured": {}}
    for window_start, window_end in windows:
        for attempt in range(1, attempts + 1):
            try:
                part = _fetch_window(client, window_start.strftime("%d/%m/%Y"),
                                     window_end.strftime("%d/%m/%Y"), cfg.get("customer_address_provinces", {}))
                break
            except ValueError as exc:
                if attempt == attempts or not str(exc).startswith(RETRYABLE):
                    raise
        overlap = customers.keys() & part["customers"].keys()
        if overlap:
            raise ValueError("Duplicate Customer catalogue ID; refusing ambiguous mapping")
        customers.update(part["customers"])
        audit["source_rows"] += part["count"]
        audit["unkeyed_rows"] += part["unkeyed"]
        audit["fields"].update(part["fields"])
        for key, n in part["structured"].items():
            audit["structured"][key] = audit["structured"].get(key, 0) + n
    end = today.strftime("%d/%m/%Y")
    count, unkeyed = audit["source_rows"], audit["unkeyed_rows"]
    fields, structured_fields = audit["fields"], audit["structured"]
    result = copy.deepcopy(cfg)
    result["customer_catalogue"] = customers
    result["customer_catalogue_audit"] = {"count": len(customers), "unkeyed_rows": unkeyed,
                                          "source_rows": count, "fields": sorted(fields),
                                          "structured_fields_not_mapped": structured_fields,
                                          "from_date": start, "to_date": end,
                                          "date_type": "cdate", "join_key": "ID=ID_khachhang"}
    result["customer_catalogue_audit"]["province_fallback_sources"] = {
        source: sum(m.get("_province_source") == source for m in customers.values())
        for source in sorted({m["_province_source"] for m in customers.values() if m.get("_province_source")})}
    return result


def _fetch_window(client: MobiWorkClient, start: str, end: str,
                  address_provinces: dict[str, str] | None = None) -> dict[str, Any]:
    params = {"tu_ngay": start, "den_ngay": end, "kieu_ngay": "cdate", "page_size": 200}
    customers: dict[str, Any] = {}
    seen_pages: set[str] = set()
    fields: set[str] = set()
    structured_fields: dict[str, int] = {}
    expected, count, unkeyed = None, 0, 0
    for page in range(1, 10_001):
        payload = client.get_json("https://openapi.mobiwork.vn/OpenAPI/V1/Customer",
                                  {**params, "page_number": page},
                                  operation_key="promotion_detail_customers", request_number=page)
        total = _api_total(payload, "Customer catalogue")
        if expected is None:
            expected = total
        elif total is not None and total != expected:
            raise ValueError("Customer catalogue total changed")
        rows = _expect_object_list(payload, "data", "Customer catalogue")
        signature = json.dumps(rows, sort_keys=True, ensure_ascii=False)
        if rows and signature in seen_pages:
            raise ValueError("Customer catalogue repeated page")
        seen_pages.add(signature)
        count += len(rows)
        for row in rows:
            fields.update(row)
            identity = value(row, "ID")
            if not identity:
                unkeyed += 1
                continue
            metadata = {}
            for label, source in FIELDS.items():
                if isinstance(row.get(source), (dict, list, bool)):
                    # Customer routes can be arrays. A current list of assigned routes
                    # does not identify the route used by a historical order.
                    structured_fields[source] = structured_fields.get(source, 0) + 1
                    metadata[label] = ""
                else:
                    metadata[label] = value(row, source)
            # Some customers still have only the legacy province field on DMS.
            # Retain its provenance: it describes the source address, not an inferred
            # province from an employee/customer-code prefix.
            if not metadata["Tỉnh"] and not isinstance(row.get("tinhthanh_pho"), (dict, list, bool)):
                metadata["Tỉnh"] = value(row, "tinhthanh_pho")
                if metadata["Tỉnh"]:
                    metadata["_province_source"] = "tinhthanh_pho"
            if not metadata["Tỉnh"]:
                # Exact, configured province suffix in this customer's DMS address.
                # Preserve the locality written there; do not infer new administrative
                # boundaries from a legacy address or use a sales/customer-code prefix.
                suffix = metadata["Địa chỉ"].rsplit(",", 1)[-1].strip().casefold()
                matches = {label for alias, label in (address_provinces or {}).items()
                           if alias.strip().casefold() == suffix and label.strip()}
                if len(matches) == 1:
                    metadata["Tỉnh"] = next(iter(matches))
                    metadata["_province_source"] = "dia_chi (địa danh ghi trong địa chỉ DMS)"
            metadata["customer_code"] = value(row, "makh")
            if identity in customers:
                raise ValueError("Duplicate Customer catalogue ID; refusing ambiguous mapping")
            customers[identity] = metadata
        if not rows or (expected is not None and count >= expected):
            break
    else:
        raise ValueError("Customer catalogue pagination safety limit")
    if expected is not None and count != expected:
        raise ValueError("Customer catalogue total mismatch")
    return {"customers": customers, "count": count, "unkeyed": unkeyed, "fields": fields,
            "structured": structured_fields}
