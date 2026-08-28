from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable, Mapping

from ..normalize import clean_text, normalize_code, to_number
from .common import Row, date_value


def standardize_flat_bom(rows: Iterable[Row]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        product = normalize_code(row.get("A"))
        material = normalize_code(row.get("B"))
        if not product or not material:
            continue
        output.append(
            {
                "product_code": product,
                "material_code": material,
                "material_name": clean_text(row.get("C")),
                "qty_per_product": to_number(row.get("D")),
            }
        )
    return output


def standardize_direct_bom(rows: Iterable[Row]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        product = normalize_code(row.get("A"))
        material = normalize_code(row.get("C"))
        if not product or not material:
            continue
        output.append(
            {
                "product_code": product,
                "product_name": clean_text(row.get("B")),
                "material_code": material,
                "material_name": clean_text(row.get("D")),
                "qty": to_number(row.get("E")),
            }
        )
    return output


def material_demand_periods(
    projection_rows: Iterable[Row],
    flat_bom_rows: Iterable[Row],
) -> dict[str, dict[str, float]]:
    projection = {normalize_code(row.get("Ma SP")): row for row in projection_rows}
    result: dict[str, dict[str, float]] = defaultdict(
        lambda: {"E": 0.0, "F": 0.0, "G": 0.0, "H": 0.0}
    )
    for row in flat_bom_rows:
        product = normalize_code(row.get("product_code"))
        material = normalize_code(row.get("material_code"))
        qty = to_number(row.get("qty_per_product"))
        src = projection.get(product)
        if not material or not src:
            continue
        result[material]["E"] += max(to_number(src.get("J Con lai")), 0.0) * qty
        result[material]["F"] += max(to_number(src.get("M FC M+1")), 0.0) * qty
        result[material]["G"] += max(to_number(src.get("N FC M+2")), 0.0) * qty
        result[material]["H"] += max(to_number(src.get("O FC M+3")), 0.0) * qty
    return dict(result)


def material_direct_projection(
    projection_rows: Iterable[Row],
    direct_bom_rows: Iterable[Row],
) -> tuple[dict[str, float], dict[str, float]]:
    projection = {normalize_code(row.get("Ma SP")): row for row in projection_rows}
    run_need: dict[str, float] = defaultdict(float)
    debt: dict[str, float] = defaultdict(float)
    for row in direct_bom_rows:
        product = normalize_code(row.get("product_code"))
        material = normalize_code(row.get("material_code"))
        qty = to_number(row.get("qty"))
        src = projection.get(product)
        if not material or not src:
            continue
        run_need[material] += to_number(src.get("L Du kien vat tu")) * qty
        debt[material] += to_number(src.get("K No kho")) * qty
    return dict(run_need), dict(debt)


def aggregate_open_po(
    po_rows: Iterable[Row],
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    totals: dict[str, float] = defaultdict(float)
    normalized: list[dict[str, Any]] = []
    for row in po_rows:
        code = normalize_code(row.get("Ma Hang"))
        if not code:
            continue
        remaining = max(
            to_number(row.get("So Luong mua")) - to_number(row.get("So Luong nhan")),
            0.0,
        )
        totals[code] += remaining
        normalized.append(
            {
                "material_code": code,
                "remaining": remaining,
                "delivery_date": date_value(row.get("Ngay Giao")),
            }
        )
    return dict(totals), normalized


def build_material_inbound_plan(
    material_rows: Iterable[Row],
    *,
    stock: Mapping[str, float],
    direct_run_need: Mapping[str, float],
    open_po: Mapping[str, float],
    material_debt: Mapping[str, float],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in material_rows:
        code = normalize_code(row.get("A"))
        if not code:
            continue
        current_stock = to_number(stock.get(code))
        run_need = to_number(direct_run_need.get(code))
        po_qty = max(to_number(open_po.get(code)), 0.0)
        debt = to_number(material_debt.get(code))
        output.append(
            {
                "Ma NVL": code,
                "Ten NVL": clean_text(row.get("B")),
                "DVT": clean_text(row.get("C")),
                "D Ton thuc te": current_stock,
                "E Vat tu cho chay so": run_need,
                "F Con thieu": max(run_need - current_stock, 0.0),
                "G Ton PO": po_qty,
                "H No kho": debt,
                "I Con thieu no kho": current_stock - debt,
            }
        )
    return output


def build_daily_material_allocation(
    material_rows: Iterable[Row],
    *,
    flat_bom_rows: Iterable[Row],
    daily_product_rows: Iterable[Row],
    stock: Mapping[str, float],
    po_lines: Iterable[Row],
    start_date: date,
    horizon_days: int = 45,
) -> list[dict[str, Any]]:
    daily_rows = list(daily_product_rows)
    qty_by_product_date: dict[tuple[str, date], float] = defaultdict(float)
    all_dates = [start_date + timedelta(days=i) for i in range(horizon_days)]
    for row in daily_rows:
        product = normalize_code(row.get("Ma SP"))
        for current in all_dates:
            qty_by_product_date[(product, current)] += to_number(
                row.get(current.isoformat())
            )

    coeff: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for row in flat_bom_rows:
        material = normalize_code(row.get("material_code"))
        product = normalize_code(row.get("product_code"))
        if material and product:
            coeff[material].append(
                (product, to_number(row.get("qty_per_product")))
            )

    po_by_material: dict[str, list[tuple[date | None, float]]] = defaultdict(list)
    for row in po_lines:
        code = normalize_code(row.get("material_code"))
        if code:
            po_by_material[code].append(
                (date_value(row.get("delivery_date")), to_number(row.get("remaining")))
            )

    output: list[dict[str, Any]] = []
    end_date = start_date + timedelta(days=horizon_days - 1)
    for material_row in material_rows:
        code = normalize_code(material_row.get("A"))
        if not code:
            continue
        demand_by_day: dict[date, float] = {}
        for current in all_dates:
            demand_by_day[current] = sum(
                qty_by_product_date.get((product, current), 0.0) * qty
                for product, qty in coeff.get(code, [])
            )
        total_demand = sum(demand_by_day.values())
        opening = to_number(stock.get(code))
        total_po = sum(max(qty, 0.0) for _, qty in po_by_material.get(code, []))
        period_po = sum(
            max(qty, 0.0)
            for delivery, qty in po_by_material.get(code, [])
            if delivery is not None and start_date <= delivery <= end_date
        )
        cumulative_demand = 0.0
        shortage: date | None = None
        for current in all_dates:
            cumulative_demand += demand_by_day[current]
            cumulative_po = sum(
                max(qty, 0.0)
                for delivery, qty in po_by_material.get(code, [])
                if delivery is not None and start_date <= delivery <= current
            )
            if cumulative_demand > opening + cumulative_po:
                shortage = current
                break
        if total_demand <= opening:
            status = "Du hang"
        elif opening + period_po >= total_demand:
            status = "Cho PO ve trong ky"
        elif opening + total_po >= total_demand:
            status = "PO co nhung can xem ngay giao"
        else:
            status = "Can dat mua them"
        note = {
            "Can dat mua them": "Can mua them de dap ung ke hoach SX trong ky.",
            "PO co nhung can xem ngay giao": (
                "PO du tong luong nhung chua co/khong du ngay giao trong ky."
            ),
            "Cho PO ve trong ky": "Da can doi theo Ngay Giao nam trong ky tren sheet PO.",
        }.get(status, "")
        output.append(
            {
                "Ma NVL": code,
                "Ten NVL": clean_text(material_row.get("B")),
                "DVT": clean_text(material_row.get("C")),
                "D Nhu cau tu ngay": total_demand,
                "E Ton dau": opening,
                "F PO mo": total_po,
                "G PO trong ky": period_po,
                "H Ngay thieu dau tien": shortage,
                "I Trang thai": status,
                "J Can mua them": max(total_demand - opening - total_po, 0.0),
                "K Ghi chu": note,
            }
        )
    return output
