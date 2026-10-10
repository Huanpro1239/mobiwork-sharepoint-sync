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
import contextlib
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
UNIT_HOLD = "Chờ xác nhận ĐVT dòng đơn"
DISPLAY_FAIL = "Không đạt (xác nhận)"  # confirmed in BoSung_Mapping/TrungBay
DISPLAY_FAILED = "Không đạt trưng bày"


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
        return any(tier.minimum <= actual and (tier.maximum == 0 or actual < tier.maximum)
                   for tier in rule.tiers) or actual >= rule.tiers[-1].minimum
    return actual >= rule.minimum


def reward_multiplier(actual: float, rule: Rule) -> int:
    if rule.tiers:
        matched = next((tier for tier in rule.tiers
                        if tier.minimum <= actual and (tier.maximum == 0 or actual < tier.maximum)), None)
        if not matched and actual >= rule.tiers[-1].minimum:
            matched = rule.tiers[-1]
        return reward_multiplier(actual, matched) if matched else 0
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


def _grading_leaves(value: Any, path: str = "", depth: int = 0) -> Iterable[tuple[str, Any]]:
    """(key path, leaf value) pairs of a grading payload of any shape (dict, list, JSON text)."""
    if depth > 6:
        return
    if isinstance(value, str) and value.strip()[:1] in ("[", "{"):
        with contextlib.suppress(ValueError):
            value = json.loads(value)
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _grading_leaves(item, f"{path}.{key}" if path else str(key), depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _grading_leaves(item, f"{path}[]", depth + 1)
    elif value is not None and not (isinstance(value, str) and not value.strip()):
        yield path, value


def grading_texts(record: dict[str, Any]) -> set[str]:
    return {str(v).strip() for _, v in _grading_leaves(record.get("cham_diem"))
            if isinstance(v, str) and v.strip()}


def display_summary(records: list[dict[str, Any]], programs: list[dict[str, Any]]) -> dict[str, Any]:
    """PII-free shape of DisplayData to verify the programme/result join."""
    wanted = {norm((p.get("cttb") or {}).get("ten")) for p in programs
              if isinstance(p.get("cttb"), dict) and p["cttb"].get("ten")}
    names = collections.Counter(norm(r.get("ten_ct")) for r in records)
    values = collections.Counter()
    keys = collections.Counter()
    shapes = collections.Counter()
    by_status = collections.Counter()
    for record in records:
        raw = record.get("cham_diem")
        shapes[type(raw).__name__ + (f"[{len(raw)}]" if isinstance(raw, (list, dict)) and len(raw) < 4 else "")] += 1
        leaves = list(_grading_leaves(raw))
        keys.update(f"{path}:{type(v).__name__}" for path, v in leaves)
        # Short labels only ("Đạt", "Không đạt", scores); long free text is never logged.
        values.update(str(v).strip() for _, v in leaves
                      if isinstance(v, (str, int, float, bool)) and 0 < len(str(v).strip()) <= 20)
        texts = {str(v).strip() for _, v in leaves if isinstance(v, str)}
        by_status[f"{record.get('tt_cham_diem')}|{'Đạt' if DISPLAY_PASS in texts else ('có chấm' if leaves else 'rỗng')}"] += 1
    fields = collections.Counter(k for r in records for k in r)
    return {"records": len(records), "programs_in_data": len(names),
        "record_fields": sorted(fields)[:30],
        "required_programs": len(wanted), "matched_programs": len(wanted & set(names)),
        "matched_records": sum(n for name, n in names.items() if name in wanted),
        "top_names": [f"{name[:45]}={n}" for name, n in names.most_common(6)],
        "required_names": sorted(name[:45] for name in wanted)[:6],
        "grading_shape": dict(shapes.most_common(6)), "status_by_grading": dict(by_status.most_common(10)),
        "grading_keys": dict(keys.most_common(10)), "grading_values": dict(values.most_common(12)),
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
        values = grading_texts(record)
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
            unit_gaps = []
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
                    unit_gaps.append(gap)
                    continue
                if line["unit"] in rule.units.get(line["sku"], ()):
                    contribution = line["amount"] if rule.kind == AMOUNT else line["quantity"]
                    actual += contribution
                    counted.append((line.get("raw"), contribution))
            meta = customers.get(customer_id, {})
            sales_meta = (sales_metadata or {}).get(customer_id, {})
            code = meta.get("customer_code") or code_of.get(customer_id, "")
            if rule.tiers:
                matched = next((tier for tier in rule.tiers
                                if tier.minimum <= actual and (tier.maximum == 0 or actual < tier.maximum)), None)
                if not matched and actual >= rule.tiers[-1].minimum:
                    matched = rule.tiers[-1]
            else:
                matched = rule if reached(actual, rule) else None
            multiplier = reward_multiplier(actual, matched) if matched else 0
            selected_rewards = matched.rewards if matched else ()
            reached_at = ""
            if matched:  # first delivery day the running total met the matched threshold
                running = 0.0
                for raw, contribution in sorted(counted, key=lambda item: str(
                        local_day((item[0] or {}).get("ngay_giao_hang")) or "")):
                    running += contribution
                    if running >= matched.minimum:
                        reached_at = str(local_day((raw or {}).get("ngay_giao_hang")) or "")
                        break
            display = ""
            if rule.display_program:
                display = (displays or {}).get((code, norm(rule.display_program)), "Chưa có dữ liệu")
            if unit_gaps:
                # The quantity of a line without unit is unknown: hold this customer only
                # (no reward, not "Không") until the unit is confirmed; others still publish.
                eligible = UNIT_HOLD
            elif multiplier == 0:
                eligible = "Không"
            elif not rule.display_program or display in {rule.display_result or DISPLAY_PASS, DISPLAY_PASS}:
                eligible = "Có"
            elif display == DISPLAY_FAIL:
                eligible = DISPLAY_FAILED
            else:
                eligible = REVIEW_DISPLAY
            proposed = {f"{sku}|{unit}": qty * multiplier
                        for sku, _, unit, qty in selected_rewards} if multiplier else {}
            has_rewards = eligible in {"Có", REVIEW_DISPLAY}
            earned_rewards = [(sku, name, unit, qty * multiplier)
                              for sku, name, unit, qty in selected_rewards] if multiplier else []
            rows.append({
                "_idCT": rule.program_id, "type": "", "ma": code or customer_id,
                "ten": meta.get("Tên Khách hàng", ""), "sdt": meta.get("Số ĐT", ""),
                "dc": meta.get("Địa chỉ", ""), "npp": sales_meta.get("Tên NPP", ""),
                "kv": meta.get("Khu vực") or meta.get("Vùng") or sales_meta.get("Vùng", ""),
                "loai": meta.get("Loại KH", ""), "nhom": meta.get("Nhóm KH", ""),
                "timepass": "", "soSuatCT": program.get("soSuat"),
                "objThucHien": {TARGET_ID: round(actual, 2)},
                "objTraThuong": proposed if has_rewards else {},
                "objThuongDuKien": proposed,
                "extra": {"Tỉnh": meta.get("Tỉnh", ""),
                          "Vùng áp dụng CT": rule.region,
                          "Trưng bày yêu cầu": rule.display_program if rule.display_program else "Không áp dụng",
                          "Kết quả trưng bày": display if rule.display_program else "Không áp dụng",
                          "Đủ điều kiện trả thưởng": eligible,
                          "Thưởng dự kiến": "; ".join(f"{name or sku} ({unit}): {qty * multiplier:g}"
                                                      for sku, name, unit, qty in selected_rewards)
                          if multiplier else "",
                          "Bội số": multiplier if rule.multiple else ""},
                "_lines": [line for line, _ in counted if line is not None],
                "_weights": [weight for line, weight in counted if line is not None],
                "_rewards": earned_rewards if has_rewards else [],
                "_eligible": eligible,
                "_review_display": eligible == REVIEW_DISPLAY,
                "_unit_gaps": unit_gaps,
                "_reached_at": reached_at,
                "_slots": multiplier,
            })
        results.append(ui.ProgramResult(program, ui.FINAL if rows else ui.EMPTY, 1, rows,
                                        [target], rewards))
    return results, issues


def unit_gap_summary(results: list[ui.ProgramResult]) -> dict[str, Any]:
    """Qualifying sale lines without unit (source keys only, never customer fields)."""
    lines: dict[tuple[str, str, str], dict[str, Any]] = {}
    held = 0
    for result in results:
        for row in result.rows:
            if row.get("_unit_gaps"):
                held += 1
            for gap in row.get("_unit_gaps") or []:
                lines.setdefault((gap["order"], gap["line"], gap["sku"]), gap)
    return {"missing_unit_line_count": len(lines), "held_customer_rows": held,
            "lines": list(lines.values())[:25]}


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
                          page_size: int = 1000, max_pages: int = 500,
                          programme: str | None = None) -> list[dict[str, Any]]:
    """DisplayData gradings in the period (paginated, repeat-page guarded).

    ``programme`` is the documented ``ten_cttb`` filter (display programme name).
    """
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        params = {"tu_ngay": first.strftime("%d/%m/%Y"), "den_ngay": last.strftime("%d/%m/%Y"),
                  "page_size": page_size, "page_number": page}
        if programme:
            params["ten_cttb"] = programme
        payload = client.get_json(
            DISPLAY_URL, params,
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


def fetch_display_for_programs(client: Any, programs: list[dict[str, Any]], first: date, last: date
                               ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Unfiltered gradings of the period plus, per required display programme, the gradings
    requested with ``ten_cttb`` over the programme period (gradings made in an earlier month
    of a multi-month programme still count). A failing programme query never hides others."""
    records = fetch_display_records(client, first, last)
    names: dict[str, date] = {}
    for program in programs:
        display = program.get("cttb") if isinstance(program.get("cttb"), dict) else {}
        name = str(display.get("ten") or "").strip()
        if not name:
            continue
        start = first
        with contextlib.suppress(KeyError, TypeError, ValueError, OSError):
            start = min(first, datetime.fromtimestamp(int(program["startDate"]) / 1000, VN_TZ).date())
        names[name] = min(names.get(name, start), start)
    stats = {"programmes": len(names), "records": 0, "graded": 0, "errors": 0}
    for name, start in sorted(names.items()):
        start = max(start, last.replace(year=last.year - 1))
        try:
            rows = fetch_display_records(client, start, last, programme=name)
        except Exception:  # one programme name the API rejects must not hide the others
            stats["errors"] += 1
            continue
        stats["records"] += len(rows)
        stats["graded"] += sum(1 for row in rows if grading_texts(row))
        records.extend(rows)
    return records, stats


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


def program_quota(program: dict[str, Any]) -> int:
    """Maximum reward slots (multiples) per customer ("Số suất"), 0 when unlimited.

    Evidence: DMS reports soSuatCT and time_soSuat per customer row; reading soSuat as a
    programme total would pay 1 of 22 qualifying customers of a monthly accumulation
    programme (008 Mức 1, soSuat 1), so it caps a customer's multiples (BoiSo).
    ``gioiHanCT`` false switches the limit off.
    """
    if program.get("gioiHanCT") is False:
        return 0
    try:
        return max(int(str(program.get("soSuat") or "").strip()), 0)
    except ValueError:
        return 0


def apply_quota(result: ui.ProgramResult, quota: int) -> dict[str, int]:
    """Cap each paid customer's multiples at ``quota`` (integer slots); other customers are unaffected."""
    stats = {"quota": quota, "reduced": 0}
    for row in result.rows:
        need = int(row.get("_slots") or 0)
        if row.get("_eligible") not in {"Có", REVIEW_DISPLAY} or need <= quota:
            continue
        ratio = quota / need
        row["objTraThuong"] = {key: round(value * ratio) for key, value in (row.get("objTraThuong") or {}).items()}
        row["_rewards"] = [(sku, name, unit, round(qty * ratio)) for sku, name, unit, qty in row.get("_rewards") or []]
        row["_slots"] = quota
        extra = row.setdefault("extra", {})
        extra["Bội số"] = quota
        extra["Giới hạn suất"] = f"{quota}/{need}"
        stats["reduced"] += 1
    return stats


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
            "display_required": bool(extra and extra[0].get("Trưng bày yêu cầu") not in ("", "Không áp dụng")),
            "display_passed": sum(1 for e in extra if e.get("Kết quả trưng bày") == DISPLAY_PASS),
            "eligible": sum(1 for e in extra if e.get("Đủ điều kiện trả thưởng") == "Có"),
            "review_display": sum(bool(row.get("_review_display", row.get("_eligible") == REVIEW_DISPLAY))
                                  for row in rows),
        })
    return out


UNREGISTERED = "Mua đạt mức CT nhưng không có trong danh sách khách đăng ký của CT trên DMS"
OTHER_UNIT = "Mua SP của CT bằng ĐVT không khai trong CT (DMS không quy đổi nên không cộng)"


def uncounted_sales(programs: list[dict[str, Any]], lines: list[dict[str, Any]],
                    regions: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Programme sales the calculator does not count, to explain a customer missing from BaoCao.

    * a buyer of the programme SKUs who is registered in no level of the programme, listed
      once per programme with the highest level its purchases alone would reach;
    * a registered customer's sales of a programme SKU in a unit its level does not declare.
    """
    by_customer: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for line in lines:
        by_customer[line["customer"]].append(line)
    families: dict[str, list[Rule]] = collections.defaultdict(list)
    for program in programs:
        with contextlib.suppress(ValueError):
            rule = parse_rule(program)
            families[program_prefix(rule.name)].append(rule)
    rows = []

    def row(rule: Rule, customer: str, reason: str, matched: list[dict[str, Any]], units: str) -> dict[str, Any]:
        raw = matched[0].get("raw") or {}
        return {"Mã CT": program_prefix(rule.name), "Mức CT": bonus_code(rule.name), "Tên CT": rule.name,
                "Vùng": (regions or {}).get(customer, ""), "Mã Khách hàng": matched[0]["code"],
                "Tên Khách hàng": ui.text(raw.get("ten_kh")), "Lý do": reason,
                "Mã SP": ", ".join(sorted({line["sku"] for line in matched})),
                "ĐVT bán": ", ".join(sorted({line["unit"] for line in matched})), "ĐVT khai trong CT": units,
                "Số lượng": sum(line["quantity"] for line in matched),
                "Thành tiền": sum(line["amount"] for line in matched),
                "Số đơn": len({ui.text((line.get("raw") or {}).get("ma_phieu")) for line in matched} - {""})}

    for rules in families.values():
        registered = set().union(*(rule.customers for rule in rules))
        skus = set().union(*(rule.units for rule in rules))

        for customer, bought in by_customer.items():
            bought = [line for line in bought if line["sku"] in skus]
            if not bought:
                continue
            if customer not in registered:
                best = None
                for rule in sorted(rules, key=lambda r: r.tiers[0].minimum if r.tiers else r.minimum):
                    counted = [line for line in bought if line["unit"] in rule.units.get(line["sku"], ())]
                    actual = sum(line["amount"] if rule.kind == AMOUNT else line["quantity"] for line in counted)
                    if counted and actual >= (rule.tiers[0].minimum if rule.tiers else rule.minimum):
                        best = (rule, counted)
                if best:
                    units = "/".join(sorted({u for us in best[0].units.values() for u in us}))
                    rows.append(row(best[0], customer, UNREGISTERED, best[1], units))
                continue
            for rule in rules:
                if customer not in rule.customers:
                    continue
                other = [line for line in bought if line["sku"] in rule.units and line["unit"]
                         and line["unit"] not in rule.units[line["sku"]]]
                if other:
                    units = "/".join(sorted({u for us in rule.units.values() for u in us}))
                    rows.append(row(rule, customer, OTHER_UNIT, other, units))
    return rows


def coverage_gaps(programs: list[dict[str, Any]], lines: list[dict[str, Any]]) -> dict[str, Any]:
    """PII-free counts of uncounted programme sales plus Bill line statuses."""
    counts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in uncounted_sales(programs, lines):
        counts[r["Mức CT"][:40]]["unregistered" if r["Lý do"] == UNREGISTERED else "other_unit"] += 1
    statuses = collections.Counter(ui.text((line.get("raw") or {}).get("trang_thai")) or "(trống)"
                                   for line in lines)
    return {"programmes": {code: dict(c) for code, c in counts.items()},
            "bill_line_statuses": dict(statuses.most_common(10))}
