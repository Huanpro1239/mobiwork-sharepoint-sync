"""Standardise order line quantities into the pack unit (Thùng / Két / Bình).

MobiWork order lines may be entered in the loose unit (e.g. Chai). The Product
catalogue declares, per SKU, the pack unit ``dvt_chan``, the loose unit ``dvt_le``
and ``hsqd`` = number of loose units in one pack. A loose-unit line is converted as
``so_luong / hsqd`` and its ``so_luong`` / ``ten_dvt`` / ``ma_dvt`` are overwritten
with the pack values. ``don_gia`` and ``thanh_tien`` stay as sent by DMS. Lines
without a known ``hsqd`` are left as sent by DMS.
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

if __package__:
    from .api_contract import api_total, expect_object_list
else:
    from api_contract import api_total, expect_object_list


PRODUCT_URL = "https://openapi.mobiwork.vn/OpenAPI/V1/Product"
PACK_UNITS = {"thùng", "két", "bình"}
CONFIG = Path(__file__).resolve().parents[1] / "config/unit_conversions.json"

LOG = logging.getLogger("mobiwork_sync")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(_text(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def fetch_product_catalogue(client: Any, operation_key: str) -> dict[str, dict[str, Any]]:
    """Read every documented Product catalogue row, keyed by ``ma_sp``."""
    seen_pages, products = set(), {}
    expected, count = None, 0
    for page in range(1, 10_001):
        payload = client.get_json(PRODUCT_URL, {"page_size": 200, "page_number": page},
                                  operation_key=operation_key, request_number=page)
        total = api_total(payload, "Product catalogue")
        if expected is None:
            expected = total
        elif total is not None and total != expected:
            raise ValueError("Product catalogue total changed")
        rows = expect_object_list(payload, "data", "Product catalogue")
        signature = json.dumps(rows, sort_keys=True, ensure_ascii=False)
        if rows and signature in seen_pages:
            raise ValueError("Product catalogue repeated page")
        seen_pages.add(signature)
        count += len(rows)
        for row in rows:
            sku = _text(row.get("ma_sp"))
            if not sku:
                # Some documented catalogue entries have null codes; they cannot map an order SKU.
                continue
            if sku in products and products[sku] != row:
                raise ValueError("Conflicting Product catalogue SKU")
            products[sku] = row
        if not rows or (expected is not None and count >= expected):
            break
    else:
        raise ValueError("Product catalogue pagination safety limit")
    if expected is not None and count != expected:
        raise ValueError("Product catalogue total mismatch")
    return products


def configured_conversions() -> dict[tuple[str, str], dict[str, Any]]:
    """Reviewed conversions kept in config (imported from the product reference file)."""
    path = Path(os.environ.get("UNIT_CONVERSIONS_CONFIG", str(CONFIG)))
    mapping = json.loads(path.read_text(encoding="utf-8")).get("unit_conversions", {})
    result = {}
    for key, value in mapping.items():
        sku, _, unit = key.partition("|")
        target = _text(value.get("target_unit"))
        per_pack = _decimal(value.get("units_per_pack"))
        if per_pack is None:
            factor = _decimal(value.get("factor"))
            per_pack = Decimal(1) / factor if factor else None
        if sku and unit and target.casefold() in PACK_UNITS and per_pack and per_pack > 0:
            result[(sku, unit.casefold())] = {"target_unit": target, "hsqd": per_pack,
                                              "source": _text(value.get("source")) or "config"}
    return result


def catalogue_conversions(products: dict[str, dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    result = {}
    for sku, row in products.items():
        large, small = _text(row.get("dvt_chan")), _text(row.get("dvt_le"))
        per_pack = _decimal(row.get("hsqd"))
        if (large.casefold() in PACK_UNITS and small and small.casefold() != large.casefold()
                and per_pack and per_pack > 0):
            result[(sku, small.casefold())] = {"target_unit": large, "hsqd": per_pack,
                                               "source": "Product.dvt_chan/dvt_le/hsqd"}
    return result


def load_conversions(client: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """DMS ``hsqd`` wins; reviewed config only fills SKUs the catalogue does not declare."""
    conversions = configured_conversions()
    conversions.update(catalogue_conversions(fetch_product_catalogue(client, "order_unit_products")))
    return conversions


def _line_unit(item: dict[str, Any]) -> str:
    return _text(item.get("ten_dvt")) or _text(item.get("ma_dvt"))


def standardise_item(item: dict[str, Any],
                     conversions: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any] | None:
    """Return the line in its pack unit, or None when it cannot be converted."""
    unit = _line_unit(item)
    if unit.casefold() in PACK_UNITS:
        return dict(item)
    conversion = conversions.get((_text(item.get("ma_sp")), unit.casefold()))
    quantity = _decimal(item.get("so_luong"))
    if not conversion or quantity is None:
        return None
    per_pack = conversion["hsqd"]
    result = dict(item)
    result["so_luong"] = float(round(quantity / per_pack, 6))
    result["ten_dvt"] = conversion["target_unit"]
    if _text(item.get("ma_dvt")):
        result["ma_dvt"] = conversion["target_unit"]
    return result


def standardise_order_units(records: list[dict[str, Any]],
                            conversions: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    result, unconverted = [], set()
    for order in records:
        order = dict(order)
        for field in ("san_pham", "san_pham_km"):
            items = order.get(field)
            if not isinstance(items, list):
                continue
            converted = []
            for item in items:
                line = standardise_item(item, conversions) if isinstance(item, dict) else item
                if line is None:
                    unconverted.add((_text(item.get("ma_sp")), _line_unit(item)))
                    line = item
                converted.append(line)
            order[field] = converted
        result.append(order)
    if unconverted:
        LOG.warning("Order lines kept in source unit (missing hsqd for Thùng/Két/Bình): %s",
                    ", ".join(f"{sku}|{unit}" for sku, unit in sorted(unconverted)))
    return result
