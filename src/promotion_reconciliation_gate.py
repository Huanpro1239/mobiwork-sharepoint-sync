"""Fail-closed structural gate for computed Promotion Bonus workbooks.

Only aggregate counters and fixed issue codes leave this module. Customer names,
telephone numbers and individual identifiers must never appear in CI diagnostics.
The gate does not approve a reward: display/registration uncertainty stays review.
"""
from __future__ import annotations

from collections import Counter
from math import isclose
from typing import Any

import pandas as pd

import promotion_bonus_calc as calc
from promotion_models import ProgramResult


class ReconciliationGateError(ValueError):
    """A workbook is unsafe to publish because its internal evidence disagrees."""


def _ensure_count(manifest: dict[str, Any], key: str, actual: int) -> None:
    if key not in manifest:
        raise ReconciliationGateError(f"missing_manifest_metric:{key}")
    value = manifest[key]
    if isinstance(value, bool) or not isinstance(value, int) or value != actual:
        raise ReconciliationGateError(
            f"manifest_count_mismatch:{key} actual={actual}"
        )


def _required_frame(
    frames: dict[str, pd.DataFrame], name: str, columns: list[str]
) -> pd.DataFrame:
    frame = frames.get(name)
    if not isinstance(frame, pd.DataFrame):
        raise ReconciliationGateError(f"missing_sheet:{name}")
    if len(frame.columns) != len(set(frame.columns)):
        raise ReconciliationGateError(f"duplicate_columns:{name}")
    if list(frame.columns) != columns and name == "DoanhSoChuaTinh":
        raise ReconciliationGateError("invalid_reconciliation_21_column_contract")
    if any(column not in frame.columns for column in columns):
        raise ReconciliationGateError(f"missing_columns:{name}")
    return frame


def _reward_amounts(ledger: pd.DataFrame, field: str) -> pd.Series:
    values = pd.to_numeric(ledger[field], errors="coerce")
    # Blank quantities in optional reward views are zero, never an approval.
    nonblank = ledger[field].notna() & ledger[field].astype(str).str.strip().ne("")
    if values[nonblank].isna().any() or values.lt(0).any():
        raise ReconciliationGateError(f"invalid_reward_amount:{field}")
    return values.fillna(0.0)


def evaluate(
    frames: dict[str, pd.DataFrame],
    results: list[ProgramResult],
    manifest: dict[str, Any],
    *,
    uncounted: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check row coverage, payout holds and reconciliation contract before publish.

    Fatal inconsistencies raise ReconciliationGateError. Ordinary business
    exceptions (unregistered buyers, missing units, pending display grading)
    produce review counters without blocking a valid operational sync.
    """
    columns = calc.RECONCILIATION_COLUMNS
    reconciliation = _required_frame(frames, "DoanhSoChuaTinh", columns)
    reward_columns = [
        "Trạng thái", "Số lượng quà dự kiến", "Số lượng quà đủ điều kiện",
        "Tiền mặt dự kiến (đ)", "Tiền mặt đủ điều kiện (đ)",
    ]
    ledger = _required_frame(frames, "TraThuong", reward_columns)

    rows = [row for result in results for row in result.rows]
    eligible = sum(row.get("_eligible") == "Có" for row in rows)
    pending_display = sum(row.get("_eligible") == calc.REVIEW_DISPLAY for row in rows)
    _ensure_count(manifest, "customer_row_count", len(rows))
    _ensure_count(manifest, "eligible_rows", eligible)
    _ensure_count(manifest, "review_display_rows", pending_display)
    _ensure_count(manifest, "uncounted_rows", len(reconciliation))

    expected = Counter(
        (calc.bonus_code(result.program_name), str(row.get("ma") or "").strip())
        for result in results for row in result.rows
    )
    if any(not code for _, code in expected):
        raise ReconciliationGateError("registered_customer_without_code")
    actual = Counter(
        (str(row["Mức CT"]).strip(), str(row["Mã khách"]).strip())
        for row in reconciliation.to_dict("records")
        if row["Trạng thái đăng ký CT"] == "Đã đăng ký"
        and row["Nguyên nhân không được tính"] != calc.OTHER_UNIT
    )
    if expected != actual:
        missing_count = sum((expected - actual).values())
        unexpected_count = sum((actual - expected).values())
        raise ReconciliationGateError(
            "registered_customer_coverage_mismatch "
            f"missing_rows={missing_count} unexpected_rows={unexpected_count}"
        )

    # The diagnostic source list is preserved separately until this check:
    # comparing it with the actual sheet catches dropped unregistered buyers
    # without exposing identities in issue text or the resulting manifest.
    if uncounted is not None:
        key_fields = ("Mức CT", "Mã khách", "Nguyên nhân không được tính")
        expected_uncounted = Counter(
            tuple(str(row.get(k) or "") for k in key_fields) for row in uncounted
        )
        actual_uncounted = Counter(
            tuple(str(row.get(k) or "") for k in key_fields)
            for row in reconciliation.to_dict("records")
            if row["Trạng thái đăng ký CT"] == "Chưa đăng ký"
            or row["Nguyên nhân không được tính"] == calc.OTHER_UNIT
        )
        if expected_uncounted != actual_uncounted:
            raise ReconciliationGateError(
                "uncounted_customer_coverage_mismatch "
                f"missing_rows={sum((expected_uncounted - actual_uncounted).values())} "
                f"unexpected_rows={sum((actual_uncounted - expected_uncounted).values())}"
            )

    for row in rows:
        if row.get("_eligible") == "Có":
            continue
        if row.get("objTraThuong") or row.get("_rewards"):
            raise ReconciliationGateError("unapproved_customer_has_confirmed_reward")

    gift_confirmed = _reward_amounts(ledger, "Số lượng quà đủ điều kiện")
    cash_confirmed = _reward_amounts(ledger, "Tiền mặt đủ điều kiện (đ)")
    gift_proposed = _reward_amounts(ledger, "Số lượng quà dự kiến")
    cash_proposed = _reward_amounts(ledger, "Tiền mặt dự kiến (đ)")
    not_eligible = ledger["Trạng thái"].ne("Có")
    if gift_confirmed[not_eligible].ne(0).any() or cash_confirmed[not_eligible].ne(0).any():
        raise ReconciliationGateError("unapproved_ledger_has_confirmed_reward")

    coverage = manifest.get("reward_coverage", {})
    if not isinstance(coverage, dict):
        raise ReconciliationGateError("invalid_reward_coverage")
    if not isclose(
        float(coverage.get("cash_eligible_vnd", float("nan"))),
        float(cash_confirmed.sum()), rel_tol=1e-9, abs_tol=0.01,
    ):
        raise ReconciliationGateError("cash_eligible_total_mismatch")
    if coverage.get("gift_reward_rows") != int(gift_confirmed.gt(0).sum()):
        raise ReconciliationGateError("gift_eligible_rows_mismatch")

    issues = Counter(
        str(reason) for reason in reconciliation["Nguyên nhân không được tính"]
        if isinstance(reason, str) and reason.strip()
    )
    deferred = int(
        (not_eligible & (gift_proposed.gt(0) | cash_proposed.gt(0))).sum()
    )
    review_codes = {
        "unregistered": int(reconciliation["Trạng thái đăng ký CT"].eq("Chưa đăng ký").sum()),
        "other_unit": int(issues.get(calc.OTHER_UNIT, 0)),
        "pending_display": pending_display,
        "missing_history": int(issues.get(calc.MISSING_HISTORY, 0)),
        "unsupported_rule": int(issues.get(calc.UNSUPPORTED_RULE, 0)),
        "unit_gap_lines": int(manifest.get("unit_gap_lines") or 0),
        "deferred_reward_lines": deferred,
    }
    needs_review = (
        any(review_codes.values())
        or not bool(manifest.get("dms_equivalence_verified", False))
        or manifest.get("quality_status") == "needs_review"
    )
    return {
        "version": 1,
        "status": "needs_review" if needs_review else "pass",
        "registered_rows_checked": sum(expected.values()),
        "reconciliation_rows": len(reconciliation),
        "eligible_customer_rows": eligible,
        "confirmed_cash_vnd": float(cash_confirmed.sum()),
        "review": review_codes,
        "dms_equivalence_verified": bool(manifest.get("dms_equivalence_verified", False)),
        "blocking_issues": 0,
    }
