from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..normalize import normalize_code, to_number
from .common import Row


def forecast_by_month(
    fc_rows: Iterable[Row],
    month: int,
    *,
    code_column: str = "B",
) -> dict[str, float]:
    if month < 1 or month > 12:
        raise ValueError(f"month must be in 1..12, got {month}")
    value_column = chr(ord("D") + month)
    result: dict[str, float] = {}
    for row in fc_rows:
        code = normalize_code(row.get(code_column))
        if code and code not in result:
            result[code] = to_number(row.get(value_column))
    return result


def build_finished_goods_projection(
    product_codes: Iterable[Any],
    *,
    stock_vikoda: Mapping[str, float],
    stock_vkd: Mapping[str, float],
    plant_stock: Mapping[str, float],
    actual_sales: Mapping[str, float],
    forecast_current: Mapping[str, float],
    forecast_m1: Mapping[str, float],
    forecast_m2: Mapping[str, float],
    forecast_m3: Mapping[str, float],
    warehouse_debt: Mapping[str, float],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in product_codes:
        code = normalize_code(raw)
        d = to_number(stock_vikoda.get(code))
        e = to_number(stock_vkd.get(code))
        f = to_number(plant_stock.get(code))
        g = d + e - f
        h = to_number(actual_sales.get(code))
        i = to_number(forecast_current.get(code))
        j = i - h - f
        k = to_number(warehouse_debt.get(code))
        material_projection = j + k if j > 0 else k
        output.append(
            {
                "Ma SP": code,
                "D Ton Vikoda": d,
                "E Ton VKD": e,
                "F Ton Nha may": f,
                "G Ton cac kho khac": g,
                "H Da ban": h,
                "I FC": i,
                "J Con lai": j,
                "K No kho": k,
                "L Du kien vat tu": material_projection,
                "M FC M+1": to_number(forecast_m1.get(code)),
                "N FC M+2": to_number(forecast_m2.get(code)),
                "O FC M+3": to_number(forecast_m3.get(code)),
            }
        )
    return output
