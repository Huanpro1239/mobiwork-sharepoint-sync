"""Compute the Promotion Bonus (trả thưởng) report from OpenAPI data only.

The observed OpenAPI PromotionBonusReport snapshot returned no customer rows.
This adapter computes supported rules from the following available inputs:

* PromotionBonus catalogue: rules, thresholds, rewards and the registered
  ``customer`` ID list of each programme;
* Bill monthly master (ChiTietSP): sold lines with delivery date, SKU, unit,
  quantity and line amount;
* Customer catalogue: customer code, type, group, province;
* DisplayData: display (trưng bày) grading results for programmes linked to a
  display programme (``cttb``).

Supported rules compared with a DMS export (docs/PROMOTION_BONUS_EVIDENCE.md);
customer listing and historical registration equivalence remain unverified:
1. only customers listed in the programme ``customer`` field;
2. only Bill lines whose delivery date (local) is in the report period;
3. only sold lines (promotion/gift lines excluded) of the listed SKUs;
4. a line counts only when its unit equals a unit declared for that SKU in the
   programme (DMS does not convert cases to bottles);
5. amount programmes add ``thanh_tien`` (đơn giá × số lượng), quantity
   programmes add ``so_luong``;
6. reached when actual >= min and (max == 0 or actual < max); multiples
   (``BoiSo``) multiply the reward by floor(actual / min).

The output reuses the DMS page row schema so promotion_bonus_ui renders the
same workbook layout.
"""
from __future__ import annotations

import collections
import json
import logging
import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from itertools import pairwise
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

import promotion_models as ui

LOG = logging.getLogger("mobiwork_promotion_bonus_calc")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
TARGET_ID = "chi_tieu"
AMOUNT = "amount"
QUANTITY = "quantity"
SUPPORTED_TYPES = {"MUTI_SP_ST_SP", "MUTI_SP_SL_SP", "SP_SL_SP", "MUTI_SP_ST_TIEN"}
MONEY_TYPE = "MUTI_SP_ST_TIEN"  # "Mua nhiều sản phẩm - đạt số tiền - tặng tiền"
MONEY_SKU, MONEY_NAME, MONEY_UNIT = "TIEN", "Tiền thưởng", "đồng"
DISPLAY_PASS = "Đạt"
REVIEW_DISPLAY = "Cần kiểm tra trưng bày"


# --------------------------------------------------------------------------- rules


@dataclass(frozen=True)
class Rule:
    program_id: str
    name: str
    ptype: str
    kind: str
    minimum: float
    maximum: float
    units: dict[str, frozenset[str]]
    product_names: dict[str, str]
    rewards: tuple[tuple[str, str, str, float], ...]  # sku, name, unit, qty
    multiple: bool
    customers: frozenset[str]
    display_program: str
    display_result: str
    region: str = ""
    tiers: tuple[Rule, ...] = ()

    @property
    def plan_text(self) -> str:
        if self.tiers:
            return " hoặc ".join(tier.plan_text.strip() for tier in self.tiers)
        def fmt(value: float) -> str:
            return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
        text = f" >= {fmt(self.minimum)}"
        if self.maximum:
            text += f" - {fmt(self.maximum)}" if self.kind == AMOUNT else f" < {fmt(self.maximum)}"
        return text

    @property
    def target_name(self) -> str:
        items = " + ".join(f"{self.product_names.get(sku, sku)}({'/'.join(sorted(units))})"
                           for sku, units in self.units.items())
        return f"{self.name} - {items}"


def _unit(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("viewData") or value.get("choice_values") or "").strip()
    return str(value or "").strip()


def _number(value: Any, label: str, *, non_negative: bool = True) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(parsed) or (non_negative and parsed < 0):
        raise ValueError(f"{label} must be finite" + (" and non-negative" if non_negative else ""))
    return parsed


def parse_rule(program: dict[str, Any]) -> Rule:
    pid = str(program.get("_id", ""))
    name = str(program.get("name", "")).strip()
    ptype = str((program.get("ptype") or {}).get("value", ""))
    if ptype not in SUPPORTED_TYPES:
        raise ValueError(f"Programme {name or pid}: unsupported type {ptype!r}")
    products = program.get("products") or []
    if len(products) > 1:
        return _tiered_rule(program)
    if len(products) != 1 or not isinstance(products[0], dict):
        raise ValueError(f"Programme {name or pid}: expected exactly one product rule")
    rule = products[0]
    units: dict[str, set[str]] = collections.defaultdict(set)
    names: dict[str, str] = {}
    if ptype == "SP_SL_SP":
        buy = [rule]
        minimum, maximum, kind = _number(rule.get("yeu_cau"), f"{name} yeu_cau"), 0.0, QUANTITY
        gifts: Iterable[dict[str, Any]] = rule.get("khuyen_mai") or []
    else:
        buy = rule.get("san_pham_mua") or []
        requirement = rule.get("yeu_cau") or {}
        if "amountMin" in requirement:
            kind = AMOUNT
            minimum = _number(requirement.get("amountMin"), f"{name} amountMin")
            maximum = _number(requirement.get("amountMax") or 0, f"{name} amountMax")
        else:
            kind = QUANTITY
            minimum = _number(requirement.get("qualityMin"), f"{name} qualityMin")
            maximum = _number(requirement.get("qualityMax") or 0, f"{name} qualityMax")
        gifts = [gift for group in rule.get("san_pham_khuyen_mai") or [] for gift in
                 (group if isinstance(group, list) else [group])]
        if (rule.get("chon_tat_ca_sp") or {}).get("chon_tat_ca_sp"):
            raise ValueError(f"Programme {name or pid}: 'all products' rules are not supported")
    for item in buy:
        sku = str(item.get("ma_san_pham", "")).strip()
        unit = _unit(item.get("don_vi_tinh"))
        if not sku or not unit:
            raise ValueError(f"Programme {name or pid}: product without code or unit")
        units[sku].add(unit)
        names.setdefault(sku, str(item.get("ten_san_pham", "")).strip())
    if not units:
        raise ValueError(f"Programme {name or pid}: no purchase products")
    rewards = tuple(
        (str(g.get("ma_san_pham", "")).strip(), str(g.get("ten_san_pham", "")).strip(),
         _unit(g.get("don_vi_tinh")), _number(g.get("so_luong"), f"{name} reward"))
        for g in gifts if isinstance(g, dict)
    )
    if ptype == MONEY_TYPE:
        # The reward is a cash amount / voucher value stored in ``khuyen_mai`` (e.g. "550000").
        rewards = ((MONEY_SKU, MONEY_NAME, MONEY_UNIT, _number(rule.get("khuyen_mai"), f"{name} khuyen_mai")),)
    display = program.get("cttb") if isinstance(program.get("cttb"), dict) else {}
    result = display.get("ket_qua") if isinstance(display.get("ket_qua"), dict) else {}
    return Rule(
        program_id=pid, name=name, ptype=ptype, kind=kind, minimum=minimum, maximum=maximum,
        units={sku: frozenset(v) for sku, v in units.items()}, product_names=names,
        rewards=rewards, multiple=bool((program.get("settings") or {}).get("BoiSo")),
        customers=frozenset(str(c) for c in program.get("customer") or [] if c),
        display_program=str(display.get("ten") or "").strip(),
        display_result=str(result.get("label") or result.get("value") or "").strip(),
        region=str((program.get("ctype") or {}).get("label") or "").strip()
        if isinstance(program.get("ctype"), dict) else "",
    )


def _tiered_rule(program: dict[str, Any]) -> Rule:
    """Adjacent, disjoint tiers over the same purchase pool, as observed in CT 355.

    Different purchase pools or overlapping requirements may mean independent or
    combined conditions. Do not reinterpret those as a single tier schedule.
    """
    tiers = sorted((parse_rule({**program, "products": [product]}) for product in program["products"]),
                   key=lambda r: r.minimum)
    first = tiers[0]
    for tier in tiers:
        if tier.kind != first.kind or tier.units != first.units:
            raise ValueError(f"Programme {first.name}: multiple rules have different purchase pools")
        if tier.minimum <= 0 or (tier.maximum and tier.maximum <= tier.minimum):
            raise ValueError(f"Programme {first.name}: invalid tier interval")
    for previous, following in pairwise(tiers):
        if not previous.maximum or previous.maximum > following.minimum:
            raise ValueError(f"Programme {first.name}: overlapping product rule intervals")
        if previous.maximum < following.minimum:
            raise ValueError(f"Programme {first.name}: gaps between product rule intervals are not supported")
    rewards = {}
    for tier in tiers:
        for reward in tier.rewards:
            rewards.setdefault((reward[0], reward[2]), reward)
    return replace(first, maximum=tiers[-1].maximum, tiers=tuple(tiers), rewards=tuple(rewards.values()))


def reached(actual: float, rule: Rule) -> bool:
    if rule.tiers:
        return any(reached(actual, tier) for tier in rule.tiers)
    return actual >= rule.minimum and (rule.maximum == 0 or actual < rule.maximum)


def reward_multiplier(actual: float, rule: Rule) -> int:
    if rule.tiers:
        return next((reward_multiplier(actual, tier) for tier in rule.tiers if reached(actual, tier)), 0)
    if not reached(actual, rule):
        return 0
    if rule.multiple and rule.minimum > 0:
        return max(1, int(actual // rule.minimum))
    return 1


# --------------------------------------------------------------------------- inputs


def local_day(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        if not text:
            return None
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)  # Bill API timestamps are UTC
    return moment.astimezone(VN_TZ).date()


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "1", "yes"}
    return bool(value) and not (isinstance(value, float) and math.isnan(value))


SALE_COLUMNS = ("ID_khachhang", "ma_kh", "ma_sp", "ten_dvt", "ma_dvt", "so_luong",
                "thanh_tien", "ngay_giao_hang", "is_km")


def sold_lines(detail: pd.DataFrame, first: date, last: date,
               unit_config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Bill ChiTietSP rows that can count toward a programme in the period."""
    from promotion_detail import source_unit

    if detail.empty:
        return []
    missing = [c for c in SALE_COLUMNS if c not in detail.columns]
    if missing:
        raise ValueError(f"Bill ChiTietSP is missing columns: {missing}")
    lines = []
    for row in detail.to_dict("records"):
        if _truthy(row.get("is_km")):
            continue
        day = local_day(row.get("ngay_giao_hang"))
        if day is None or not first <= day <= last:
            continue
        customer, sku = ui.text(row.get("ID_khachhang")), ui.text(row.get("ma_sp"))
        unit, _ = source_unit(row, False, unit_config)
        if not customer or not sku:
            raise ValueError("Bill sale line is missing customer ID or product code")
        lines.append({"raw": row, "customer": customer,
                      "code": ui.text(row.get("ma_kh")), "sku": sku, "unit": unit,
                      "quantity": _number(row.get("so_luong"), "Bill quantity", non_negative=False),
                      "amount": _number(row.get("thanh_tien"), "Bill amount", non_negative=False)})
    return lines


def norm(text: Any) -> str:
    """Case/space-insensitive key for names typed differently across DMS screens."""
    return " ".join(str(text or "").split()).casefold()


def display_summary(records: list[dict[str, Any]], programs: list[dict[str, Any]]) -> dict[str, Any]:
    """PII-free shape of DisplayData to verify the programme/result join."""
    wanted = {norm((p.get("cttb") or {}).get("ten")) for p in programs
              if isinstance(p.get("cttb"), dict) and p["cttb"].get("ten")}
    names = collections.Counter(norm(r.get("ten_ct")) for r in records)
    values = collections.Counter()
    keys = collections.Counter()
    for record in records:
        grading = record.get("cham_diem") if isinstance(record.get("cham_diem"), dict) else {}
        keys.update(grading.keys())
        values.update(str(v).strip() for v in grading.values() if isinstance(v, str) and v.strip())
    fields = collections.Counter(k for r in records for k in r)
    return {"records": len(records), "programs_in_data": len(names),
            "record_fields": sorted(fields)[:30],
            "required_programs": len(wanted), "matched_programs": len(wanted & set(names)),
            "matched_records": sum(n for name, n in names.items() if name in wanted),
            "top_names": [f"{name[:45]}={n}" for name, n in names.most_common(6)],
            "required_names": sorted(name[:45] for name in wanted)[:6],
            "grading_keys": dict(keys.most_common(6)), "grading_values": dict(values.most_common(8)),
            "status_values": dict(collections.Counter(str(r.get("tt_cham_diem")) for r in records))}


def display_passes(records: Iterable[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """(customer code, normalised display programme name) -> grading status text.

    "Đạt" only when a grading value literally says so; otherwise the raw grading
    state is reported, never interpreted.
    """
    rank = {DISPLAY_PASS: 3}
    best: dict[tuple[str, str], str] = {}
    for record in records:
        code = str(record.get("ma_kh") or "").strip()
        program = norm(record.get("ten_ct"))
        if not code or not program:
            continue
        grading = record.get("cham_diem") if isinstance(record.get("cham_diem"), dict) else {}
        values = {str(v).strip() for v in grading.values() if isinstance(v, str) and v.strip()}
        state = record.get("tt_cham_diem")
        if DISPLAY_PASS in values:
            result = DISPLAY_PASS
        elif values:
            result = sorted(values)[0]
        elif state not in (None, ""):
            result = f"Đã ghi nhận (trạng thái {state})"
        else:
            result = "Đã ghi nhận, chưa chấm"
        current = best.get((code, program))
        if current is None or rank.get(result, 1 if result.startswith("Đã ghi nhận (") else 0) > \
                rank.get(current, 1 if current.startswith("Đã ghi nhận (") else 0):
            best[(code, program)] = result
    return best


# --------------------------------------------------------------------------- compute


def compute(
    programs: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    customers: dict[str, dict[str, Any]],
    *,
    displays: dict[tuple[str, str], str] | None = None,
    sales_metadata: dict[str, dict[str, str]] | None = None,
) -> tuple[list[ui.ProgramResult], list[dict[str, Any]]]:
    """Return per-programme results in the DMS row schema plus rule issues."""
    by_customer: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    code_of: dict[str, str] = {}
    for line in lines:
        by_customer[line["customer"]].append(line)
        if line["code"]:
            code_of.setdefault(line["customer"], line["code"])
    results: list[ui.ProgramResult] = []
    issues: list[dict[str, Any]] = []
    missing_units: dict[tuple[str, str, str], dict[str, Any]] = {}
    for program in programs:
        try:
            rule = parse_rule(program)
        except ValueError as exc:
            issues.append({"Chương trình": program.get("name"), "Mã CT (id)": program.get("_id"),
                           "Vấn đề": str(exc)})
            results.append(ui.ProgramResult(program, "unsupported", 0))
            continue
        target = {"_id": TARGET_ID, "_idCT": rule.program_id, "ten": rule.target_name,
                  "kh": rule.plan_text, "min": rule.minimum, "max": rule.maximum}
        rewards = [{"_id": f"{sku}|{unit}", "ten": f"{name or sku}({unit})"}
                   for sku, name, unit, _ in rule.rewards]
        rows = []
        for customer_id in sorted(rule.customers):
            actual = 0.0
            counted = []
            for line in by_customer.get(customer_id, []):
                if line["sku"] in rule.units and not line["unit"]:
                    raw = line.get("raw") or {}
                    key = (ui.text(raw.get("ma_phieu")), ui.text(raw.get("stt")), line["sku"])
                    gap = missing_units.setdefault(key, {"order": key[0], "line": key[1],
                                                         "sku": key[2], "quantity": line["quantity"],
                                                         "programmes": []})
                    code = bonus_code(rule.name)
                    if code not in gap["programmes"]:
                        gap["programmes"].append(code)
                    continue
                if line["unit"] in rule.units.get(line["sku"], ()):
                    contribution = line["amount"] if rule.kind == AMOUNT else line["quantity"]
                    actual += contribution
                    counted.append((line.get("raw"), contribution))
            meta = customers.get(customer_id, {})
            sales_meta = (sales_metadata or {}).get(customer_id, {})
            code = meta.get("customer_code") or code_of.get(customer_id, "")
            matched = next((tier for tier in rule.tiers or (rule,) if reached(actual, tier)), None)
            multiplier = reward_multiplier(actual, matched) if matched else 0
            selected_rewards = matched.rewards if matched else ()
            display = ""
            if rule.display_program:
                display = (displays or {}).get((code, norm(rule.display_program)), "Chưa có dữ liệu")
            if multiplier == 0:
                eligible = "Không"
            elif not rule.display_program or display in {rule.display_result or DISPLAY_PASS, DISPLAY_PASS}:
                eligible = "Có"
            else:
                eligible = REVIEW_DISPLAY
            proposed = {f"{sku}|{unit}": qty * multiplier
                        for sku, _, unit, qty in selected_rewards} if multiplier else {}
            rows.append({
                "_idCT": rule.program_id, "type": "", "ma": code or customer_id,
                "ten": meta.get("Tên Khách hàng", ""), "sdt": meta.get("Số ĐT", ""),
                "dc": meta.get("Địa chỉ", ""), "npp": sales_meta.get("Tên NPP", ""),
                "kv": meta.get("Khu vực") or meta.get("Vùng") or sales_meta.get("Vùng", ""),
                "loai": meta.get("Loại KH", ""), "nhom": meta.get("Nhóm KH", ""),
                "timepass": "", "soSuatCT": program.get("soSuat"),
                "objThucHien": {TARGET_ID: round(actual, 2)},
                "objTraThuong": proposed if eligible == "Có" else {},
                "objThuongDuKien": proposed,
                "extra": {"Tỉnh": meta.get("Tỉnh", ""),
                          "Vùng áp dụng CT": rule.region,
                          "Trưng bày yêu cầu": rule.display_program,
                          "Kết quả trưng bày": display,
                          "Đủ điều kiện trả thưởng": eligible,
                          "Thưởng dự kiến": "; ".join(f"{name or sku} ({unit}): {qty * multiplier:g}"
                                                      for sku, name, unit, qty in selected_rewards)
                          if multiplier else "",
                          "Bội số": multiplier if rule.multiple else ""},
                "_lines": [line for line, _ in counted if line is not None],
                "_weights": [weight for line, weight in counted if line is not None],
                "_rewards": [(sku, name, unit, qty * multiplier) for sku, name, unit, qty in selected_rewards]
                if eligible == "Có" else [],
                "_eligible": eligible,
                "_review_display": eligible == REVIEW_DISPLAY,
            })
        results.append(ui.ProgramResult(program, ui.FINAL if rows else ui.EMPTY, 1, rows,
                                        [target], rewards))
    if missing_units:
        # Do not return/publish partial calculations. Expose only the source keys
        # needed to confirm units, never raw customer identity or contact fields.
        details = {"missing_unit_line_count": len(missing_units),
                   "lines": list(missing_units.values())[:25]}
        raise ValueError("Qualifying Bill sale line is missing its unit: "
                         + json.dumps(details, ensure_ascii=False))
    return results, issues


def program_prefix(name: str) -> str:
    """Business code shared by all levels/tiers of a programme, e.g. "246/TB/GT/04/2026"."""
    import re

    match = re.match(r"^\s*(\d+/TB/GT/\d+/\d{4}(?:_Q[1-4])?)", name or "")
    return match.group(1) if match else (name or "").strip()


def bonus_code(name: str) -> str:
    """Business code as written in the DMS template, keeping the level (Mức n)."""
    import re

    match = re.match(r"^\s*(\d+/TB/GT/\d+/\d{4}(?:_Q[1-4])?)", name or "")
    if not match:
        return (name or "").strip()
    level = re.search(r"M[ỨỨứU]C\s*(\d+|[A-Z])\b", name, flags=re.IGNORECASE)
    if level:
        return f"{match.group(1)} - Mức {level.group(1).upper()}"
    tier = re.search(r"LO[ẠA]I\s*([A-Z])\b", name, flags=re.IGNORECASE)
    return f"{match.group(1)} - Loại {tier.group(1).upper()}" if tier else match.group(1)


GIFT_ORDER = "TRẢ THƯỞNG"
BONUS_TEXT, BONUS_VALUE = "_bonus_text", "_bonus_value"


def allocate(weights: list[float], total: float, digits: int) -> list[float]:
    """Split ``total`` over lines in proportion to their contribution to the target.

    Rounded to ``digits``; the rounding remainder goes to the largest contributor so the
    allocations add up exactly to ``total``. Equal split when contributions do not add up
    to a positive number.
    """
    if not weights:
        return []
    base = sum(weights)
    shares = [w / base for w in weights] if base > 0 else [1 / len(weights)] * len(weights)
    parts = [round(total * share, digits) for share in shares]
    biggest = max(range(len(weights)), key=lambda i: shares[i])
    parts[biggest] = round(total - sum(p for i, p in enumerate(parts) if i != biggest), digits)
    return parts


def average_prices(lines: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    """Average selling price (thành tiền / số lượng) per (SKU, unit) over sold lines."""
    totals: dict[tuple[str, str], list[float]] = collections.defaultdict(lambda: [0.0, 0.0])
    for line in lines:
        if line["quantity"] > 0 and line["amount"] > 0:
            item = totals[(line["sku"], line["unit"])]
            item[0] += line["amount"]
            item[1] += line["quantity"]
    return {key: amount / qty for key, (amount, qty) in totals.items() if qty > 0}


def _per_order_rewards(row: dict[str, Any], prices: dict[tuple[str, str], float]
                       ) -> tuple[list[str | None], list[float | None], list[float], list[float | None]]:
    """Keep known cash separate from estimated (possibly unpriced) physical gifts."""
    weights = row.get("_weights") or [1.0] * len(row.get("_lines") or [])
    count = len(row.get("_lines") or [])
    texts: list[list[str]] = [[] for _ in range(count)]
    cash = [0.0] * count
    gifts: list[float | None] = [0.0] * count
    for sku, name, unit, qty in row.get("_rewards") or []:
        if sku == MONEY_SKU:
            for index, part in enumerate(allocate(weights, qty, 0)):
                cash[index] += part
            continue
        else:
            for index, part in enumerate(allocate(weights, qty, 4)):
                texts[index].append(f"{name or sku} ({unit}): {part:g}")
            price = prices.get((sku, unit))
            value_total = qty * price if price else None
        if value_total is None:
            gifts = [None] * count  # retain cash even when a gift has no selling price
            continue
        for index, part in enumerate(allocate(weights, value_total, 0)):
            if gifts[index] is not None:
                gifts[index] += part
    values = [None if gift is None else gift + money for gift, money in zip(gifts, cash, strict=True)]
    return ["; ".join(t) or None for t in texts], values, cash, gifts


def detail_source(results: list[ui.ProgramResult], label: str,
                  prices: dict[tuple[str, str], float] | None = None) -> list[tuple[str, pd.DataFrame]]:
    """Bill-shaped rows per programme for the DMS CTKM template.

    Each counted sale line is repeated under its programme code; an eligible customer
    gets one synthetic gift line per reward product (order id ``TRẢ THƯỞNG``). The paid
    reward is also allocated to the counted sale lines (``_bonus_text``/``_bonus_value``)
    in proportion to each line's contribution to the target.
    """
    out = []
    for result in results:
        code = bonus_code(result.program_name)
        rows: list[dict[str, Any]] = []
        for row in result.rows:
            lines = row.get("_lines") or []
            texts, values, cash, gifts = (_per_order_rewards(row, prices or {}) if row.get("_rewards")
                                         else tuple([None] * len(lines) for _ in range(4)))
            for line, bonus_text, bonus_value, money, gift in zip(lines, texts, values, cash, gifts, strict=True):
                rows.append({**line, "ctkm": code, "promotion": None, "ctkmFull_id": None,
                             "ctkmFull_ten_khuyen_mai": None, "is_km": False, "loai_hang": "Bán hàng",
                             BONUS_TEXT: bonus_text, BONUS_VALUE: bonus_value,
                             "_cash_alloc": money, "_gift_alloc": gift, "_cash_reward": None})
            if not lines or not row.get("_rewards"):
                continue
            base = dict(lines[-1])
            for index, (sku, name, unit, qty) in enumerate(row["_rewards"], start=1):
                rows.append({**base, BONUS_TEXT: None, BONUS_VALUE: None,
                             "_cash_alloc": None, "_gift_alloc": None,
                             "_cash_reward": qty if sku == MONEY_SKU else None,
                             "ma_phieu": GIFT_ORDER, "stt": f"{row['ma']}-{index}",
                             "_money_reward": sku == MONEY_SKU,
                             "ctkm": code, "promotion": None, "ctkmFull_id": None,
                             "ctkmFull_ten_khuyen_mai": None, "is_km": True, "loai_hang": "Khuyến mãi",
                             "ma_sp": sku, "ten_sp": name,
                             "so_luong": qty, "ten_dvt": unit, "ma_dvt": unit,
                             "ma_sp_km": None, "ten_sp_km": None, "so_luong_km": None,
                             "ma_dvt_km": None, "ten_dvt_km": None, "ngay_dat": base.get("ngay_dat")})
        if rows:
            out.append((code, pd.DataFrame(rows, dtype=object)))
    return out


def customer_sales_metadata(
    lines: list[dict[str, Any]], employees: dict[str, dict[str, str]],
    cfg: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, str]], int]:
    """Use unambiguous sales assignments (order warehouse NPP, else employee), never
    programme eligibility regions."""
    candidates: dict[str, dict[str, set[str]]] = collections.defaultdict(
        lambda: collections.defaultdict(set))
    if cfg is not None:
        from promotion_detail import resolve_sales
    for line in lines:
        raw = line.get("raw") or {}
        employee = (resolve_sales(raw, cfg) if cfg is not None
                    else employees.get(ui.text(raw.get("ma_nv_dat")), {}))
        for field in ("Vùng", "Tên NPP"):
            value = ui.text(employee.get(field))
            if value:
                candidates[line["customer"]][field].add(value)
    result, conflicts = {}, 0
    for customer, fields in candidates.items():
        result[customer] = {}
        for field, values in fields.items():
            if len(values) == 1:
                result[customer][field] = next(iter(values))
            else:
                conflicts += 1
    return result, conflicts


DISPLAY_URL = "https://openapi.mobiwork.vn/OpenAPI/V1/DisplayData"


def fetch_display_records(client: Any, first: date, last: date,
                          page_size: int = 1000, max_pages: int = 500) -> list[dict[str, Any]]:
    """All DisplayData gradings in the period (paginated, repeat-page guarded)."""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        payload = client.get_json(
            DISPLAY_URL,
            {"tu_ngay": first.strftime("%d/%m/%Y"), "den_ngay": last.strftime("%d/%m/%Y"),
             "page_size": page_size, "page_number": page},
            operation_key="promotion_bonus_display", request_number=page,
        )
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ValueError("DisplayData response is missing data array")
        rows = [row for row in rows if isinstance(row, dict)]
        signature = repr([(r.get("ma_kh"), r.get("ten_ct"), r.get("ngay_cap_nhat")) for r in rows])
        if rows and signature in seen:
            raise ValueError("DisplayData repeated page; refusing incomplete pagination")
        seen.add(signature)
        records.extend(rows)
        if len(rows) < page_size:
            return records
    raise ValueError("DisplayData pagination safety limit exceeded")


def provisional(results: list[ui.ProgramResult], end: date) -> None:
    """Accumulation not finished yet: keep progress, hold the reward until ``end``."""
    label = f"Tạm tính – CT kết thúc {end:%d/%m/%Y}"
    for result in results:
        for row in result.rows:
            if row.get("_eligible") == "Có":
                row["_eligible"] = label
                row["extra"]["Đủ điều kiện trả thưởng"] = label
                row["objTraThuong"] = {}
                row["_rewards"] = []


def hold_rewards(results: list[ui.ProgramResult], reason: str) -> None:
    """Keep customer progress visible while required calculation inputs are missing."""
    for result in results:
        for row in result.rows:
            row["_eligible"] = reason
            row["extra"]["Đủ điều kiện trả thưởng"] = reason
            row["objTraThuong"] = {}
            row["_rewards"] = []


def diagnostics(results: list[ui.ProgramResult]) -> list[dict[str, Any]]:
    out = []
    for result in results:
        rows = result.rows
        extra = [row.get("extra") or {} for row in rows]
        out.append({
            "id": result.program_id, "name": result.program_name[:80],
            "registered": len(rows),
            "with_sales": sum(1 for r in rows if (r.get("objThucHien") or {}).get(TARGET_ID, 0) > 0),
            "reached": sum(1 for r in rows if r.get("objThuongDuKien")),
            "display_required": bool(extra and extra[0].get("Trưng bày yêu cầu")),
            "display_passed": sum(1 for e in extra if e.get("Kết quả trưng bày") == DISPLAY_PASS),
            "eligible": sum(1 for e in extra if e.get("Đủ điều kiện trả thưởng") == "Có"),
            "review_display": sum(bool(row.get("_review_display", row.get("_eligible") == REVIEW_DISPLAY))
                                  for row in rows),
        })
    return out
