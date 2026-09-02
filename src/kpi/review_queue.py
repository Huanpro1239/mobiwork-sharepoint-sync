"""Pure helpers for separating AI scoring state from human review state."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from kpi.manual_labels import ManualLabelIndex, safe_text


_TIERED_AUTO_PASS_STATUSES = frozenset(
    {
        "TIER1_HIGH_PASS",
        "TIER2_EVIDENCE_PASS",
        "TIER2_CONSENSUS_PASS",
        "TIER4_WEIGHTED_PASS",
    }
)
_TIERED_AUTO_FAIL_STATUSES = frozenset(
    {
        "TIER0_AUTO_FAIL_FRAUD",
        "TIER3_CLEAR_FAIL",
        "TIER4_WEIGHTED_FAIL",
    }
)
_TIERED_REVIEW_STATUSES = frozenset(
    {
        "TIER0_REVIEW_FRAUD",
        "TIER4_WEIGHTED_REVIEW",
    }
)

SCOPE_CURRENT_KPI = "KPI_THANG_HIEN_TAI"
SCOPE_FRAUD_AUDIT = "AUDIT_FRAUD"
SCOPE_DEFERRED = "DE_DUYET_SAU"
SCOPE_HISTORICAL = "REVIEW_LICH_SU"
SCOPE_RESOLVED = "DA_DUYET_TAY"
SCOPE_PENDING = "CHO_CHAM_AI"
SCOPE_TECHNICAL = "LOI_KY_THUAT"
SCOPE_AUTOMATIC = "AI_TU_DONG"


@dataclass(frozen=True)
class ReviewPartitions:
    """Disjoint operational queues exposed to the KPI workbook."""

    manual_required: pd.DataFrame
    manual_resolved: pd.DataFrame
    fraud_audit: pd.DataFrame
    deferred_review: pd.DataFrame
    historical_review: pd.DataFrame
    pending: pd.DataFrame
    technical: pd.DataFrame


def _records_with_manual_labels(
    frame: pd.DataFrame,
    manual_labels: ManualLabelIndex,
) -> pd.DataFrame:
    enriched = frame.copy()
    if enriched.empty:
        enriched["_manual_label"] = pd.Series(dtype="object")
        return enriched
    enriched["_manual_label"] = [
        manual_labels.lookup(record)
        for record in enriched.to_dict(orient="records")
    ]
    return enriched


def _normalised_statuses(frame: pd.DataFrame) -> pd.Series:
    return frame.get(
        "Trạng Thái Quyết Định",
        pd.Series("", index=frame.index, dtype="object"),
    ).map(lambda value: safe_text(value).strip().upper())


def _decision_masks(statuses: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return review/auto-pass/auto-fail masks for legacy and tiered policies."""

    is_review = statuses.str.startswith("REVIEW_", na=False) | statuses.isin(
        _TIERED_REVIEW_STATUSES
    )
    is_auto_pass = statuses.str.startswith("AUTO_PASS", na=False) | statuses.isin(
        _TIERED_AUTO_PASS_STATUSES
    )
    is_auto_fail = statuses.str.startswith("AUTO_FAIL", na=False) | statuses.isin(
        _TIERED_AUTO_FAIL_STATUSES
    )
    return is_review, is_auto_pass, is_auto_fail


def _fraud_review_mask(statuses: pd.Series) -> pd.Series:
    return statuses.eq("TIER0_REVIEW_FRAUD") | statuses.str.contains(
        "REVIEW_FRAUD", na=False
    )


def _period_mask(frame: pd.DataFrame, period_start) -> pd.Series:
    """Return rows belonging to the KPI month; unknown dates fail safe as current."""

    if period_start is None:
        return pd.Series(True, index=frame.index, dtype="bool")
    period = pd.Timestamp(period_start).to_period("M")
    dates = pd.to_datetime(
        frame.get("ngay", pd.Series(pd.NaT, index=frame.index)),
        errors="coerce",
    )
    return dates.isna() | dates.dt.to_period("M").eq(period)


def _scene_key(record: dict[str, object]) -> tuple[str, str]:
    customer = safe_text(record.get("ma_kh")).strip().casefold()
    scene = safe_text(record.get("Loại Cảnh")).strip()
    return customer, scene


def annotate_review_workflow(
    frame: pd.DataFrame,
    manual_labels: ManualLabelIndex,
    *,
    period_start=None,
) -> pd.DataFrame:
    """Annotate rows with the operational action needed in the Excel workbook.

    The scorer still keeps every raw review decision for audit/model learning.
    Operational manual review is narrower: previous-month reviews do not block the
    current KPI month, and a non-fraud review is deferrable when the same customer
    already has an automatic pass for the same scene in the current month.
    """

    enriched = _records_with_manual_labels(frame, manual_labels)
    statuses = _normalised_statuses(enriched)
    has_manual = enriched["_manual_label"].map(bool)
    is_review, is_auto_pass, _ = _decision_masks(statuses)
    is_fraud_review = _fraud_review_mask(statuses)
    is_current = _period_mask(enriched, period_start)

    scopes = pd.Series(SCOPE_AUTOMATIC, index=enriched.index, dtype="object")
    priorities = pd.Series("", index=enriched.index, dtype="object")
    reasons = pd.Series("AI đã có quyết định tự động.", index=enriched.index, dtype="object")

    pending_mask = statuses.eq("PENDING_SCORE")
    technical_mask = statuses.eq("TECHNICAL_FAILURE")
    scopes.loc[pending_mask] = SCOPE_PENDING
    priorities.loc[pending_mask] = "CHỜ HỆ THỐNG"
    reasons.loc[pending_mask] = "Ảnh chưa tới lượt chấm AI; không cần người dùng xử lý."

    scopes.loc[technical_mask] = SCOPE_TECHNICAL
    priorities.loc[technical_mask] = "KỸ THUẬT"
    reasons.loc[technical_mask] = "Lỗi kỹ thuật; không được sửa nhãn thay cho việc sửa hệ thống."

    resolved_mask = is_review & has_manual
    scopes.loc[resolved_mask] = SCOPE_RESOLVED
    priorities.loc[resolved_mask] = "ĐÃ XONG"
    reasons.loc[resolved_mask] = "Đã có nhãn sửa tay hợp lệ và sẽ được bảo toàn qua lần xuất sau."

    unresolved = is_review & ~has_manual
    fraud_mask = unresolved & is_fraud_review
    scopes.loc[fraud_mask] = SCOPE_FRAUD_AUDIT
    priorities.loc[fraud_mask] = "CAO"
    reasons.loc[fraud_mask] = "Có tín hiệu gian lận/đối phó; giữ riêng cho audit, không trộn với review KPI thông thường."

    current_unresolved = unresolved & is_current & ~is_fraud_review
    historical_mask = unresolved & ~is_current & ~is_fraud_review
    scopes.loc[historical_mask] = SCOPE_HISTORICAL
    priorities.loc[historical_mask] = "THẤP"
    reasons.loc[historical_mask] = "Ảnh thuộc tháng trước; không chặn KPI tháng hiện tại."

    current_pass_keys: set[tuple[str, str]] = set()
    if bool((is_auto_pass & is_current).any()):
        for record in enriched.loc[is_auto_pass & is_current].to_dict(orient="records"):
            key = _scene_key(record)
            if key[0] and key[1]:
                current_pass_keys.add(key)

    redundant_mask = pd.Series(False, index=enriched.index, dtype="bool")
    for index, record in enriched.loc[current_unresolved].iterrows():
        key = _scene_key(record.to_dict())
        if key[0] and key[1] and key in current_pass_keys:
            redundant_mask.loc[index] = True

    deferred_mask = current_unresolved & redundant_mask
    scopes.loc[deferred_mask] = SCOPE_DEFERRED
    priorities.loc[deferred_mask] = "THẤP"
    reasons.loc[deferred_mask] = "Cùng khách hàng đã có ảnh Đạt tự động đúng loại cảnh trong tháng; ảnh này không cần duyệt để xác định KPI."

    required_mask = current_unresolved & ~redundant_mask
    scopes.loc[required_mask] = SCOPE_CURRENT_KPI
    priorities.loc[required_mask] = "CAO"
    reasons.loc[required_mask] = "Ảnh tháng hiện tại còn mơ hồ và có thể làm thay đổi điều kiện ảnh của KPI."

    enriched["_review_scope"] = scopes
    enriched["_review_priority"] = priorities
    enriched["_review_action_reason"] = reasons
    return enriched


def partition_review_rows(
    frame: pd.DataFrame,
    manual_labels: ManualLabelIndex,
    *,
    period_start=None,
) -> ReviewPartitions:
    """Partition manual work by business impact instead of AI label alone."""

    enriched = annotate_review_workflow(
        frame,
        manual_labels,
        period_start=period_start,
    )
    scope = enriched["_review_scope"]

    return ReviewPartitions(
        manual_required=enriched.loc[scope.eq(SCOPE_CURRENT_KPI)].copy(),
        manual_resolved=enriched.loc[scope.eq(SCOPE_RESOLVED)].copy(),
        fraud_audit=enriched.loc[scope.eq(SCOPE_FRAUD_AUDIT)].copy(),
        deferred_review=enriched.loc[scope.eq(SCOPE_DEFERRED)].copy(),
        historical_review=enriched.loc[scope.eq(SCOPE_HISTORICAL)].copy(),
        pending=enriched.loc[scope.eq(SCOPE_PENDING)].copy(),
        technical=enriched.loc[scope.eq(SCOPE_TECHNICAL)].copy(),
    )


def _unique_count(frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0

    identities: list[str] = []
    for position, record in enumerate(frame.to_dict(orient="records")):
        image_sha = safe_text(record.get("image_sha256")).strip()
        url = safe_text(record.get("hinh_anh")).strip()
        record_id = safe_text(record.get("record_id")).strip()
        identities.append(url or image_sha or record_id or f"row:{position}")
    return len(set(identities))


def _value_counts(frame: pd.DataFrame, column: str) -> dict[str, int]:
    if column not in frame.columns:
        return {}
    values = frame[column].map(lambda value: safe_text(value).strip() or "(blank)")
    return {str(key): int(value) for key, value in values.value_counts().items()}


def summarize_review_rows(
    frame: pd.DataFrame,
    manual_labels: ManualLabelIndex,
    *,
    current_pipeline_signature: str = "",
    period_start=None,
) -> dict[str, Any]:
    """Return model-quality metrics plus the smaller operational review workload."""

    partitions = partition_review_rows(
        frame,
        manual_labels,
        period_start=period_start,
    )
    statuses = _normalised_statuses(frame)
    is_review, is_auto_pass, is_auto_fail = _decision_masks(statuses)
    is_scored = is_review | is_auto_pass | is_auto_fail

    scored_count = int(is_scored.sum())
    manual_decision_count = int(is_review.sum())
    auto_pass_count = int(is_auto_pass.sum())
    auto_fail_count = int(is_auto_fail.sum())

    current_mask = pd.Series(True, index=frame.index, dtype="bool")
    if current_pipeline_signature:
        signatures = frame.get(
            "pipeline_signature",
            pd.Series("", index=frame.index, dtype="object"),
        ).map(lambda value: safe_text(value).strip())
        current_mask = signatures.eq(current_pipeline_signature)
    current_scored = is_scored & current_mask
    current_review = is_review & current_mask
    current_auto_pass = is_auto_pass & current_mask
    current_scored_count = int(current_scored.sum())
    current_manual_rate = (
        float(current_review.sum()) / current_scored_count
        if current_scored_count
        else 0.0
    )
    current_auto_pass_rate = (
        float(current_auto_pass.sum()) / current_scored_count
        if current_scored_count
        else 0.0
    )

    month_mask = _period_mask(frame, period_start)
    month_scored = is_scored & month_mask
    month_scored_count = int(month_scored.sum())
    operational_count = int(
        len(partitions.manual_required) + len(partitions.fraud_audit)
    )
    operational_rate = (
        operational_count / month_scored_count if month_scored_count else 0.0
    )

    warnings: list[str] = []
    if current_scored_count >= 500 and (
        current_auto_pass_rate < 0.20 or current_manual_rate > 0.80
    ):
        warnings.append("LOW_PRODUCTION_AUTO_COVERAGE")

    return {
        "row_count": int(len(frame)),
        "unique_image_count": _unique_count(frame),
        "scored_decision_count": scored_count,
        "manual_review_decision_count": manual_decision_count,
        "manual_review_required_count": int(len(partitions.manual_required)),
        "manual_review_required_unique": _unique_count(partitions.manual_required),
        "manual_review_resolved_count": int(len(partitions.manual_resolved)),
        "manual_review_resolved_unique": _unique_count(partitions.manual_resolved),
        "fraud_audit_required_count": int(len(partitions.fraud_audit)),
        "fraud_audit_required_unique": _unique_count(partitions.fraud_audit),
        "deferred_review_count": int(len(partitions.deferred_review)),
        "historical_review_count": int(len(partitions.historical_review)),
        "operational_review_required_count": operational_count,
        "current_period_scored_count": month_scored_count,
        "current_period_operational_review_rate": operational_rate,
        "pending_score_count": int(len(partitions.pending)),
        "pending_score_unique": _unique_count(partitions.pending),
        "technical_failure_count": int(len(partitions.technical)),
        "technical_failure_unique": _unique_count(partitions.technical),
        "auto_pass_count": auto_pass_count,
        "auto_fail_count": auto_fail_count,
        "manual_review_rate": manual_decision_count / scored_count if scored_count else 0.0,
        "auto_pass_rate": auto_pass_count / scored_count if scored_count else 0.0,
        "current_model_scored_count": current_scored_count,
        "current_model_manual_review_rate": current_manual_rate,
        "current_model_auto_pass_rate": current_auto_pass_rate,
        "label_counts": _value_counts(frame, "Phân Loại AI"),
        "decision_status_counts": _value_counts(frame, "Trạng Thái Quyết Định"),
        "warnings": warnings,
    }
