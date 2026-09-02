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


@dataclass(frozen=True)
class ReviewPartitions:
    """Disjoint queues that must not be combined in the review workbook."""

    manual_required: pd.DataFrame
    manual_resolved: pd.DataFrame
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


def partition_review_rows(
    frame: pd.DataFrame,
    manual_labels: ManualLabelIndex,
) -> ReviewPartitions:
    """Partition operational queues using decision status, never the AI label.

    ``Khong_the_cham`` is retained as a legacy display label for pending rows, so
    using ``Phân Loại AI`` here would incorrectly turn backlog and source errors
    into human review work.
    """

    enriched = _records_with_manual_labels(frame, manual_labels)
    statuses = _normalised_statuses(enriched)
    has_manual = enriched["_manual_label"].map(bool)
    is_review, _, _ = _decision_masks(statuses)

    return ReviewPartitions(
        manual_required=enriched.loc[is_review & ~has_manual].copy(),
        manual_resolved=enriched.loc[is_review & has_manual].copy(),
        pending=enriched.loc[statuses.eq("PENDING_SCORE")].copy(),
        technical=enriched.loc[statuses.eq("TECHNICAL_FAILURE")].copy(),
    )


def _unique_count(frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0

    identities: list[str] = []
    for position, record in enumerate(frame.to_dict(orient="records")):
        image_sha = safe_text(record.get("image_sha256")).strip()
        url = safe_text(record.get("hinh_anh")).strip()
        record_id = safe_text(record.get("record_id")).strip()
        # Operational queues and retries are keyed by exact URL. SHA remains a
        # fallback only for records whose source URL is unavailable.
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
) -> dict[str, Any]:
    """Return row/unique counts with pending and technical rows excluded from rates."""

    partitions = partition_review_rows(frame, manual_labels)
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
