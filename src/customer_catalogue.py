from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from mobiwork import MobiWorkClient
from promotion_bonus import _api_total, _expect_object_list

FIELDS = {"Tỉnh": "tinh_thanh_moi", "Loại KH": "loai_kh", "Route": "code_router",
          "Tên Khách hàng": "tenkh", "Địa chỉ": "dia_chi", "Số ĐT": "sdt"}


def value(row: dict[str, Any], field: str) -> str:
    raw = row.get(field)
    if raw is None:
        return ""
    if isinstance(raw, (dict, list, bool)):
        raise ValueError(f"Customer catalogue {field} must be scalar text")
    return str(raw).strip()


def enrich_customer_config(client: MobiWorkClient, cfg: dict[str, Any]) -> dict[str, Any]:
    """Join current customer metadata by the observed API ID, never by name or code alone."""
    start = cfg.get("customer_catalogue_start_date", "01/01/1900")
    datetime.strptime(start, "%d/%m/%Y")
    end = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%d/%m/%Y")
    params = {"tu_ngay": start, "den_ngay": end, "kieu_ngay": "cdate", "page_size": 200}
    customers: dict[str, Any] = {}
    seen_pages: set[str] = set()
    fields: set[str] = set()
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
            metadata = {label: value(row, source) for label, source in FIELDS.items()}
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
    result = copy.deepcopy(cfg)
    result["customer_catalogue"] = customers
    result["customer_catalogue_audit"] = {"count": len(customers), "unkeyed_rows": unkeyed,
                                          "source_rows": count, "fields": sorted(fields),
                                          "from_date": start, "to_date": end,
                                          "date_type": "cdate", "join_key": "ID=ID_khachhang"}
    return result
