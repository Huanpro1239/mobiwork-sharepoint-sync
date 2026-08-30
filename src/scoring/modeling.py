"""Balanced classifier heads and group-aware out-of-fold evaluation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedGroupKFold

from .config import (
    QUALITY_GATE_MIN_AUTO_FAIL_SAMPLES,
    QUALITY_GATE_MIN_AUTO_PASS_COVERAGE,
    QUALITY_GATE_MIN_GROUPS_PER_SUBCATEGORY,
    QUALITY_GATE_MIN_PRECISION,
)
from .decision_policy import DEFAULT_POLICY, DecisionPolicy, ScoreVector, decide_scores


FINAL_LABELS = ("Bien_hieu", "Trung_bay", "Khong_dat")
PREDICTION_LABELS = FINAL_LABELS + ("Can_duyet",)


@dataclass(frozen=True)
class TrainedHeads:
    scene: LogisticRegression
    sign_validity: LogisticRegression
    display_validity: LogisticRegression
    fraud: LogisticRegression

    @property
    def names(self) -> tuple[str, ...]:
        return ("scene", "sign_validity", "display_validity", "fraud")


@dataclass(frozen=True)
class ModelScoreBatch:
    sign_probability: np.ndarray
    sign_pass_probability: np.ndarray
    display_pass_probability: np.ndarray
    pass_probability: np.ndarray
    fraud_probability: np.ndarray


@dataclass(frozen=True)
class EvaluationReport:
    folds: int
    total_count: int
    auto_decided_count: int
    review_count: int
    auto_pass_count: int
    auto_fail_count: int
    correct_auto_count: int
    coverage: float
    auto_pass_coverage: float
    accuracy_on_auto: float
    auto_pass_precision: float
    auto_fail_precision: float
    balanced_accuracy: float
    confusion_labels: tuple[str, ...]
    confusion_matrix: tuple[tuple[int, ...], ...]
    per_subcategory_recall: dict[str, float]
    group_counts: dict[str, int]

    @property
    def quality_gate_passed(self) -> bool:
        return passes_quality_gate(self)

    @property
    def auto_fail_gate_passed(self) -> bool:
        return passes_auto_fail_gate(self)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["quality_gate_passed"] = self.quality_gate_passed
        payload["auto_fail_gate_passed"] = self.auto_fail_gate_passed
        return payload


def _fit_binary(features: np.ndarray, targets: np.ndarray) -> LogisticRegression:
    target_values = np.asarray(targets, dtype=np.int8)
    if np.unique(target_values).size != 2:
        raise ValueError("Every logistic head requires both binary classes")
    model = LogisticRegression(
        C=1.0,
        class_weight="balanced",
        max_iter=3000,
        solver="lbfgs",
        random_state=20260828,
    )
    return model.fit(np.asarray(features, dtype=np.float32), target_values)


def _label_masks(subcategories: Sequence[str]):
    labels = np.asarray(subcategories, dtype=str)
    lowered = np.char.lower(labels)
    sign = np.char.find(lowered, "bien hieu") >= 0
    display = np.char.find(lowered, "trung bay") >= 0
    fraud = np.char.find(lowered, "doi pho") >= 0
    passed = np.char.startswith(lowered, "dat/")
    return labels, sign, display, fraud, passed


def train_heads(embeddings: np.ndarray, subcategories: Sequence[str]) -> TrainedHeads:
    features = np.asarray(embeddings, dtype=np.float32)
    labels, sign, display, fraud, passed = _label_masks(subcategories)
    if features.ndim != 2 or features.shape[0] != labels.shape[0]:
        raise ValueError("Embeddings and subcategories must have matching rows")
    scene_rows = ~fraud
    return TrainedHeads(
        scene=_fit_binary(features[scene_rows], sign[scene_rows]),
        sign_validity=_fit_binary(features[sign], passed[sign]),
        display_validity=_fit_binary(features[display], passed[display]),
        fraud=_fit_binary(features, fraud),
    )


def _positive_probability(model: LogisticRegression, embeddings: np.ndarray) -> np.ndarray:
    classes = np.asarray(model.classes_)
    positive_columns = np.flatnonzero(classes == 1)
    if positive_columns.size != 1:
        raise ValueError("Binary head does not contain the positive class")
    return model.predict_proba(embeddings)[:, int(positive_columns[0])]


def score_embeddings(heads: TrainedHeads, embeddings: np.ndarray) -> ModelScoreBatch:
    features = np.asarray(embeddings, dtype=np.float32)
    if features.ndim == 1:
        features = features.reshape(1, -1)
    sign_probability = _positive_probability(heads.scene, features)
    sign_pass = _positive_probability(heads.sign_validity, features)
    display_pass = _positive_probability(heads.display_validity, features)
    fraud_probability = _positive_probability(heads.fraud, features)
    pass_probability = np.where(sign_probability >= 0.5, sign_pass, display_pass)
    return ModelScoreBatch(
        sign_probability=sign_probability,
        sign_pass_probability=sign_pass,
        display_pass_probability=display_pass,
        pass_probability=pass_probability,
        fraud_probability=fraud_probability,
    )


def _reference_similarity(
    query_embeddings: np.ndarray,
    reference_embeddings: np.ndarray,
    neighbours: int = 3,
) -> np.ndarray:
    queries = np.asarray(query_embeddings, dtype=np.float32)
    references = np.asarray(reference_embeddings, dtype=np.float32)
    if not len(references):
        return np.zeros(len(queries), dtype=np.float32)
    neighbour_count = min(neighbours, len(references))
    result = np.empty(len(queries), dtype=np.float32)
    for start in range(0, len(queries), 256):
        similarities = queries[start : start + 256] @ references.T
        top = np.partition(
            similarities,
            similarities.shape[1] - neighbour_count,
            axis=1,
        )[:, -neighbour_count:]
        result[start : start + len(similarities)] = np.mean(top, axis=1)
    return result


def _final_label(subcategory: str) -> str:
    normalised = str(subcategory).casefold()
    if normalised.startswith("dat/bien hieu"):
        return "Bien_hieu"
    if normalised.startswith("dat/trung bay"):
        return "Trung_bay"
    return "Khong_dat"


def _group_counts(subcategories: np.ndarray, groups: np.ndarray) -> dict[str, int]:
    return {
        label: int(np.unique(groups[subcategories == label]).size)
        for label in sorted(np.unique(subcategories).tolist())
    }


def _build_report(
    subcategories: np.ndarray,
    groups: np.ndarray,
    predictions: np.ndarray,
    folds: int,
) -> EvaluationReport:
    truth = np.asarray([_final_label(value) for value in subcategories], dtype=str)
    predicted = np.asarray(predictions, dtype=str)
    auto_mask = predicted != "Can_duyet"
    pass_mask = np.isin(predicted, ("Bien_hieu", "Trung_bay"))
    fail_mask = predicted == "Khong_dat"
    correct = predicted == truth
    total = len(truth)
    auto_count = int(np.count_nonzero(auto_mask))
    auto_pass_count = int(np.count_nonzero(pass_mask))
    auto_fail_count = int(np.count_nonzero(fail_mask))
    correct_auto = int(np.count_nonzero(correct & auto_mask))
    confusion = []
    for true_label in FINAL_LABELS:
        confusion.append(
            tuple(
                int(
                    np.count_nonzero(
                        (truth == true_label) & (predicted == predicted_label)
                    )
                )
                for predicted_label in PREDICTION_LABELS
            )
        )
    class_recalls = []
    for label in FINAL_LABELS:
        class_rows = truth == label
        if np.any(class_rows):
            class_recalls.append(float(np.mean(predicted[class_rows] == label)))
    per_subcategory_recall = {}
    for subcategory in sorted(np.unique(subcategories).tolist()):
        rows = subcategories == subcategory
        expected = _final_label(subcategory)
        per_subcategory_recall[subcategory] = float(
            np.mean(predicted[rows] == expected)
        )
    return EvaluationReport(
        folds=folds,
        total_count=total,
        auto_decided_count=auto_count,
        review_count=total - auto_count,
        auto_pass_count=auto_pass_count,
        auto_fail_count=auto_fail_count,
        correct_auto_count=correct_auto,
        coverage=(auto_count / total) if total else 0.0,
        auto_pass_coverage=(auto_pass_count / total) if total else 0.0,
        accuracy_on_auto=(correct_auto / auto_count) if auto_count else 0.0,
        auto_pass_precision=float(np.mean(correct[pass_mask])) if auto_pass_count else 0.0,
        auto_fail_precision=float(np.mean(correct[fail_mask])) if auto_fail_count else 0.0,
        balanced_accuracy=float(np.mean(class_recalls)) if class_recalls else 0.0,
        confusion_labels=PREDICTION_LABELS,
        confusion_matrix=tuple(confusion),
        per_subcategory_recall=per_subcategory_recall,
        group_counts=_group_counts(subcategories, groups),
    )


def cross_validate_heads(
    embeddings: np.ndarray,
    subcategories: Sequence[str],
    groups: Sequence[str],
    folds: int = 5,
    policy: DecisionPolicy = DEFAULT_POLICY,
) -> EvaluationReport:
    """Evaluate every row out-of-fold while keeping groups fully isolated."""

    features = np.asarray(embeddings, dtype=np.float32)
    labels = np.asarray(subcategories, dtype=str)
    group_values = np.asarray(groups, dtype=str)
    if features.ndim != 2 or not (
        features.shape[0] == labels.shape[0] == group_values.shape[0]
    ):
        raise ValueError("Embeddings, subcategories, and groups must have matching rows")
    if not len(labels):
        raise ValueError("Cannot evaluate an empty reference dataset")
    if folds < 2:
        raise ValueError("At least two folds are required")
    counts = _group_counts(labels, group_values)
    minimum_groups = min(counts.values())
    if minimum_groups < 2:
        predictions = np.full(len(labels), "Can_duyet", dtype="U16")
        return _build_report(labels, group_values, predictions, folds=0)
    actual_folds = min(int(folds), minimum_groups)
    splitter = StratifiedGroupKFold(
        n_splits=actual_folds,
        shuffle=True,
        random_state=20260828,
    )
    predictions = np.full(len(labels), "Can_duyet", dtype="U16")
    evaluated = np.zeros(len(labels), dtype=bool)
    for train_indices, validation_indices in splitter.split(
        features,
        labels,
        group_values,
    ):
        heads = train_heads(features[train_indices], labels[train_indices])
        model_scores = score_embeddings(heads, features[validation_indices])
        similarities = _reference_similarity(
            features[validation_indices],
            features[train_indices],
        )
        for position, row_index in enumerate(validation_indices):
            decision = decide_scores(
                ScoreVector(
                    sign_probability=float(model_scores.sign_probability[position]),
                    pass_probability=float(model_scores.pass_probability[position]),
                    fraud_probability=float(model_scores.fraud_probability[position]),
                    reference_similarity=float(similarities[position]),
                ),
                policy,
            )
            predictions[row_index] = decision.label
            evaluated[row_index] = True
    if not np.all(evaluated):
        missing = int(np.count_nonzero(~evaluated))
        raise RuntimeError(f"Group-aware evaluation skipped {missing} reference rows")
    return _build_report(labels, group_values, predictions, folds=actual_folds)


def _has_enough_validation_groups(report: EvaluationReport) -> bool:
    return bool(report.group_counts) and all(
        count >= QUALITY_GATE_MIN_GROUPS_PER_SUBCATEGORY
        for count in report.group_counts.values()
    )


def passes_quality_gate(report: EvaluationReport) -> bool:
    """Gate automatic passes on independently measured OOF precision and coverage."""

    return (
        _has_enough_validation_groups(report)
        and report.auto_pass_precision >= QUALITY_GATE_MIN_PRECISION
        and report.auto_pass_coverage >= QUALITY_GATE_MIN_AUTO_PASS_COVERAGE
    )


def passes_auto_fail_gate(report: EvaluationReport) -> bool:
    """Gate automatic failures separately so sparse fail evidence cannot reject KPI rows."""

    return (
        _has_enough_validation_groups(report)
        and report.auto_fail_count >= QUALITY_GATE_MIN_AUTO_FAIL_SAMPLES
        and report.auto_fail_precision >= QUALITY_GATE_MIN_PRECISION
    )
