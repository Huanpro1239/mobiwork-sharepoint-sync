from __future__ import annotations

import argparse
import hashlib
import json
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

REQUIRED = ["MaSanPhamMoi_Vikoda", "MaSanPhamMoi_VKD", "Quy cách", "ĐVT", "Brand", "Group"]


def code(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise ValueError("Invalid boolean product code")
    if isinstance(value, (int, float)):
        if not float(value).is_integer():
            raise ValueError("Product code must not contain a fractional part")
        return str(int(value))
    return str(value).strip()


def read_reference(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    products: dict[str, Any] = {}
    conversions: dict[str, Any] = {}
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook["DanhMucSanPham"]
        rows = iter(sheet.values)
        headers = list(next(rows))
        if any(label not in headers for label in REQUIRED):
            raise ValueError("Product reference is missing required headers")
        for row_number, row in enumerate(rows, 2):
            values = dict(zip(headers, row, strict=True))
            codes = {code(values[label]) for label in REQUIRED[:2]} - {""}
            if not codes:
                continue
            metadata = {"Brand": str(values["Brand"] or "").strip(),
                        "Package": str(values["Group"] or "").strip()}
            if not all(metadata.values()):
                raise ValueError(f"Product reference row {row_number}: missing Brand/Group")
            for sku in codes:
                if sku in products and products[sku] != metadata:
                    raise ValueError(f"Conflicting reference metadata for SKU={sku}")
                products[sku] = metadata
            unit = unicodedata.normalize("NFC", str(values["ĐVT"] or "")).strip()
            if not unit and values["Quy cách"] is None:
                # Semi-finished products can have brand metadata without a pack definition.
                continue
            if unit.casefold() not in {"thùng", "két", "bình"}:
                raise ValueError(f"Product reference row {row_number}: invalid pack unit")
            try:
                size = Decimal(str(values["Quy cách"]))
            except InvalidOperation as exc:
                raise ValueError(f"Product reference row {row_number}: invalid pack size") from exc
            if not size.is_finite() or size <= 0 or size != size.to_integral_value():
                raise ValueError(f"Product reference row {row_number}: invalid pack size")
            conversion = {"target_unit": unit, "factor": str(Decimal(1) / size),
                          "units_per_pack": str(size), "source": path.name,
                          "source_row": row_number}
            # DMS Bill uses Chai for the beverage lines observed in the authenticated export.
            # Preserve a distinct Lon mapping for products explicitly classified as cans.
            loose_units = ["Chai"]
            if str(values.get("Brand 1") or "").strip().casefold() == "lon":
                loose_units.append("Lon")
            for sku in codes:
                for loose in loose_units:
                    key = f"{sku}|{loose}"
                    previous = conversions.get(key)
                    if previous and any(previous[field] != conversion[field] for field in ("target_unit", "factor")):
                        raise ValueError(f"Conflicting pack size for SKU={sku}")
                    conversions[key] = conversion
    finally:
        workbook.close()
    return products, conversions


def update_config(source: Path, destination: Path) -> dict[str, int]:
    products, conversions = read_reference(source)
    cfg = json.loads(destination.read_text(encoding="utf-8"))
    cfg.setdefault("products", {}).update(products)
    existing = cfg.setdefault("unit_conversions", {})
    for key in list(existing):
        if key.split("|", 1)[0] in products:
            del existing[key]
    existing.update(conversions)
    cfg["product_reference"] = {"filename": source.name,
                                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                                "sheet": "DanhMucSanPham",
                                "product_code_count": len(products),
                                "codes_without_pack": sorted(set(products) - {key.split("|", 1)[0] for key in conversions})}
    staged = destination.with_suffix(".json.tmp")
    try:
        staged.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staged.replace(destination)
    finally:
        staged.unlink(missing_ok=True)
    return {"product_codes": len(products), "unit_conversions": len(conversions)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config/promotion_detail.json"))
    args = parser.parse_args()
    print(json.dumps(update_config(args.source, args.config)))
