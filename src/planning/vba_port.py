from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import ceil
from typing import Any, Iterable, Mapping, Sequence

from .normalize import clean_text, normalize_code, normalize_compare_text, remove_vietnamese_accents, to_number

Row = Mapping[str, Any]


@dataclass(frozen=True)
class PurchaseSuggestion:
    material_code: str
    net_requirement: float
    suggested_order: float
    status: str


def build_divisor_map(dmsp_rows: Iterable[Row]) -> dict[str, float]:
    """DMSP C -> F; first duplicate wins, matching modCommon."""
    result: dict[str, float] = {}
    for row in dmsp_rows:
        code = normalize_code(row.get("C"))
        if code and code not in result:
            result[code] = to_number(row.get("F"))
    return result


def load_source_stock_first(rows: Iterable[Row], value_column: str, *, code_column: str = "B", code_mode: str = "none") -> dict[str, float]:
    result: dict[str, float] = {}
    for row in rows:
        code = normalize_code(row.get(code_column), code_mode)
        if code and code not in result:
            result[code] = to_number(row.get(value_column))
    return result


def apply_divided_stock(destination_codes: Iterable[Any], source_values: Mapping[str, float], divisors: Mapping[str, float], *, code_mode: str = "none") -> dict[str, float]:
    out: dict[str, float] = {}
    for raw in destination_codes:
        code = normalize_code(raw, code_mode)
        divisor = to_number(divisors.get(code))
        value = to_number(source_values.get(code))
        out[normalize_code(raw)] = value / divisor if divisor else 0.0
    return out


def aggregate_sales_actual(source1_rows: Iterable[Row], source2_rows: Iterable[Row]) -> dict[tuple[str, str], float]:
    totals: dict[tuple[str, str], float] = defaultdict(float)

    def add(row: Row, source2: bool) -> None:
        code = normalize_code(row.get("O"))
        channel = clean_text(row.get("A"))
        if not code or not channel:
            return
        if source2:
            if normalize_compare_text(row.get("LoaiHoaDon")) != "HOADONBAN":
                return
            if clean_text(row.get("K")).upper() == "VKD3":
                return
        totals[(code, normalize_compare_text(channel))] += to_number(row.get("Q"))

    for row in source1_rows:
        add(row, False)
    for row in source2_rows:
        add(row, True)
    return dict(totals)


def sales_channels_in_cases(product_code: Any, channel_headers: Sequence[Any], raw_totals: Mapping[tuple[str, str], float], divisor: float) -> dict[str, float]:
    code = normalize_code(product_code)
    if not divisor:
        return {clean_text(h): 0.0 for h in channel_headers}
    result: dict[str, float] = {}
    for header in channel_headers:
        label = clean_text(header)
        key = normalize_compare_text(header)
        if key in {"KAMT", "KA/MT"}:
            qty = raw_totals.get((code, "KA"), 0.0) + raw_totals.get((code, "MT"), 0.0)
        else:
            qty = raw_totals.get((code, key), 0.0)
        result[label] = to_number(qty) / divisor
    return result


def aggregate_gui_kho(rows: Iterable[Row]) -> dict[str, float]:
    result: dict[str, float] = defaultdict(float)
    for row in rows:
        code = normalize_code(row.get("J"))
        if code:
            result[code] += to_number(row.get("AG"))
    return dict(result)


def map_gui_kho_to_products(destination_codes: Iterable[Any], source: Mapping[str, float]) -> dict[str, float]:
    result: dict[str, float] = {}
    for raw in destination_codes:
        dest = normalize_code(raw)
        result[dest] = to_number(source.get(normalize_code(dest, "1to2")))
    return result


def nokho_col_d(sum_rows: Iterable[Row], destination_codes: Iterable[Any]) -> dict[str, float]:
    latest: dict[str, float] = {}
    for row in sum_rows:
        code = normalize_code(row.get("B"))
        if code:
            latest[code] = to_number(row.get("U"))
    return {normalize_code(c): to_number(latest.get(normalize_code(c))) for c in destination_codes}


def nokho_col_e(stock_rows: Iterable[Row], destination_codes: Iterable[Any]) -> dict[str, float]:
    latest: dict[str, float] = {}
    for row in stock_rows:
        code = normalize_code(row.get("C"))
        if code:
            latest[code] = to_number(row.get("G")) + to_number(row.get("O"))
    return {normalize_code(c): to_number(latest.get(normalize_code(c))) for c in destination_codes}


def nokho_balance(col_d: Mapping[str, float], col_e: Mapping[str, float], col_f: Mapping[str, float]) -> dict[str, float]:
    codes = set(col_d) | set(col_e) | set(col_f)
    return {code: max(to_number(col_d.get(code)) - to_number(col_e.get(code)) + to_number(col_f.get(code)), 0.0) for code in codes}


def sum_two_divided_stocks(destination_codes: Iterable[Any], ao_rows: Iterable[Row], ap_rows: Iterable[Row], divisors: Mapping[str, float], *, value_column: str) -> dict[str, float]:
    ao = load_source_stock_first(ao_rows, value_column, code_mode="none")
    ap = load_source_stock_first(ap_rows, value_column, code_mode="2to1")
    out: dict[str, float] = {}
    for raw in destination_codes:
        code = normalize_code(raw)
        divisor = to_number(divisors.get(code))
        out[code] = (to_number(ao.get(code)) + to_number(ap.get(code))) / divisor if divisor else 0.0
    return out


def extract_open_po(rows: Iterable[Row]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        if clean_text(row.get("AB")):
            continue
        output.append({
            "Ma Hang": normalize_code(row.get("F")),
            "Ten Hang": row.get("G"),
            "DVT": row.get("H"),
            "So Luong mua": to_number(row.get("I")),
            "So Luong nhan": to_number(row.get("K")),
            "Ngay Dat": row.get("D"),
            "Ngay Giao": row.get("W"),
        })
    return output


def aggregate_xuat_kho(source1_rows: Iterable[Row], source2_rows: Iterable[Row]) -> list[dict[str, Any]]:
    totals: dict[tuple[str, str, str], float] = defaultdict(float)

    def add(rows: Iterable[Row], mode: str) -> None:
        for row in rows:
            if not clean_text(row.get("I")):
                continue
            code = normalize_code(row.get("R"), mode)
            if not code:
                continue
            name = clean_text(row.get("S"))
            unit = clean_text(row.get("T"))
            totals[(code, name, unit)] += to_number(row.get("U"))

    add(source1_rows, "none")
    add(source2_rows, "1to2")
    return [{"MASANPHAM": code, "TENSANPHAM": name, "DONVITINH": unit, "TONG SO LUONG XUAT": qty} for (code, name, unit), qty in totals.items()]


def map_ton_tt(source_rows: Iterable[Row], destination_codes: Iterable[Any], *, special_code: str = "130100149", special_keyword: str = "nap van ket") -> dict[str, float]:
    dest = {normalize_code(c) for c in destination_codes if normalize_code(c)}
    out = {code: 0.0 for code in dest}
    keyword = remove_vietnamese_accents(special_keyword).lower()
    for row in source_rows:
        code = normalize_code(row.get("C"))
        total = to_number(row.get("N")) + to_number(row.get("O"))
        if code in out and out[code] == 0:
            out[code] = total
        if special_code in out:
            name = remove_vietnamese_accents(row.get("E")).lower()
            if keyword and keyword in name:
                out[special_code] += total
    return out


def finished_goods_need(product_codes: Iterable[Any], stock_vikoda: Mapping[str, float], stock_vkd: Mapping[str, float], plant_stock: Mapping[str, float], actual_sales: Mapping[str, float], forecast: Mapping[str, float], warehouse_debt: Mapping[str, float]) -> list[dict[str, float | str]]:
    rows: list[dict[str, float | str]] = []
    for raw in product_codes:
        code = normalize_code(raw)
        d = to_number(stock_vikoda.get(code)); e = to_number(stock_vkd.get(code)); f = to_number(plant_stock.get(code))
        g = d + e - f
        h = to_number(actual_sales.get(code)); i = to_number(forecast.get(code)); j = i - h - f
        k = to_number(warehouse_debt.get(code)); l = j + k if j > 0 else k
        rows.append({"code": code, "D_stock_vikoda": d, "E_stock_vkd": e, "F_plant_stock": f, "G_other_stock": g, "H_actual_sales": h, "I_forecast": i, "J_remaining": j, "K_warehouse_debt": k, "L_material_projection": l})
    return rows


def explode_bom(bom_rows: Iterable[Row], finished_product_codes: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    children: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    parent_names: dict[str, str] = {}
    for row in bom_rows:
        parent = normalize_code(row.get("parent_code")); child = normalize_code(row.get("child_code"))
        if not parent or not child:
            continue
        parent_names[parent] = clean_text(row.get("parent_name"))
        children[parent].append((child, clean_text(row.get("child_name")), to_number(row.get("qty"))))
    roots = [normalize_code(x) for x in (finished_product_codes or children.keys())]
    output: list[dict[str, Any]] = []

    def walk(current: str, multiplier: float, path: tuple[str, ...], acc: dict[str, tuple[str, float]]) -> None:
        if current in path:
            raise ValueError(f"BOM cycle detected: {' -> '.join((*path, current))}")
        next_path = (*path, current)
        for child, child_name, qty in children.get(current, []):
            combined = multiplier * qty
            if child in children:
                walk(child, combined, next_path, acc)
            else:
                old_name, old_qty = acc.get(child, (child_name, 0.0))
                acc[child] = (old_name or child_name, old_qty + combined)

    for root in roots:
        if root not in children:
            continue
        acc: dict[str, tuple[str, float]] = {}
        walk(root, 1.0, tuple(), acc)
        for material, (name, qty) in acc.items():
            output.append({"product_code": root, "product_name": parent_names.get(root, ""), "material_code": material, "material_name": name, "qty_per_product": qty})
    return output


def material_requirement(finished_need: Mapping[str, float], flat_bom_rows: Iterable[Row]) -> dict[str, float]:
    result: dict[str, float] = defaultdict(float)
    for row in flat_bom_rows:
        product = normalize_code(row.get("product_code")); material = normalize_code(row.get("material_code"))
        if product and material:
            result[material] += to_number(finished_need.get(product)) * to_number(row.get("qty_per_product"))
    return dict(result)


def purchase_suggestions(requirements: Mapping[str, float], stock: Mapping[str, float], open_po: Mapping[str, float], moq: Mapping[str, float]) -> list[PurchaseSuggestion]:
    output: list[PurchaseSuggestion] = []
    for code in sorted(set(requirements) | set(stock) | set(open_po)):
        need = max(to_number(requirements.get(code)) - to_number(stock.get(code)) - to_number(open_po.get(code)), 0.0)
        lot = to_number(moq.get(code))
        suggested = ceil(need / lot) * lot if need > 0 and lot > 0 else need
        output.append(PurchaseSuggestion(code, need, suggested, "Can dat mua them" if suggested > 0 else "Du hang"))
    return output
