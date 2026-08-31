"""Pure decision policy for converting model scores into auditable labels.

This module deliberately has no dependency on computer-vision runtimes.  The
model proposes a decision first; detector/OCR evidence may confirm an automatic
pass or downgrade it to review, but can never manufacture a pass or failure.
"""

from dataclasses import dataclass, replace

from scoring.config import (
    AUTO_FAIL_MAX,
    AUTO_PASS_MIN,
    FRAUD_AUTO_FAIL_MIN,
    FRAUD_REVIEW_MIN,
    REFERENCE_SIMILARITY_MIN,
    SCENE_MARGIN_MIN,
)


def _require_finite_range(name: str, value: float, minimum: float, maximum: float) -> None:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be numeric") from error
    if not minimum <= numeric <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


@dataclass(frozen=True)
class DecisionPolicy:
    auto_pass_min: float = AUTO_PASS_MIN
    auto_fail_max: float = AUTO_FAIL_MAX
    fraud_auto_fail_min: float = FRAUD_AUTO_FAIL_MIN
    fraud_review_min: float = FRAUD_REVIEW_MIN
    scene_margin_min: float = SCENE_MARGIN_MIN
    reference_similarity_min: float = REFERENCE_SIMILARITY_MIN

    def __post_init__(self) -> None:
        _require_finite_range("auto_pass_min", self.auto_pass_min, 0.0, 1.0)
        _require_finite_range("auto_fail_max", self.auto_fail_max, 0.0, 1.0)
        _require_finite_range("fraud_auto_fail_min", self.fraud_auto_fail_min, 0.0, 1.0)
        _require_finite_range("fraud_review_min", self.fraud_review_min, 0.0, 1.0)
        _require_finite_range("scene_margin_min", self.scene_margin_min, 0.0, 0.5)
        _require_finite_range("reference_similarity_min", self.reference_similarity_min, -1.0, 1.0)
        if self.auto_fail_max >= self.auto_pass_min:
            raise ValueError("auto_fail_max must be lower than auto_pass_min")
        if self.fraud_review_min >= self.fraud_auto_fail_min:
            raise ValueError("fraud_review_min must be lower than fraud_auto_fail_min")


@dataclass(frozen=True)
class ScoreVector:
    sign_probability: float
    pass_probability: float
    fraud_probability: float
    reference_similarity: float

    def __post_init__(self) -> None:
        _require_finite_range("sign_probability", self.sign_probability, 0.0, 1.0)
        _require_finite_range("pass_probability", self.pass_probability, 0.0, 1.0)
        _require_finite_range("fraud_probability", self.fraud_probability, 0.0, 1.0)
        _require_finite_range("reference_similarity", self.reference_similarity, -1.0, 1.0)


@dataclass(frozen=True)
class DetectorEvidence:
    has_signboard: bool = False
    has_brand_keyword: bool = False
    has_bottle_or_pack: bool = False
    has_face: bool = False


@dataclass(frozen=True)
class ScoringDecision:
    label: str
    scene: str
    status: str
    score: float
    reasons: tuple[str, ...]


DEFAULT_POLICY = DecisionPolicy()


def decide_scores(
    scores: ScoreVector,
    policy: DecisionPolicy = DEFAULT_POLICY,
    scene_override: str | None = None,
) -> ScoringDecision:
    """Apply conservative thresholds to calibrated model probabilities.

    ``scene_override`` is reserved for a separately-audited scene resolver.  It
    may choose which validity head is evaluated, but it never bypasses novelty,
    fraud, validity thresholds, or quality gates.
    """

    if scene_override not in (None, "Bien_hieu", "Trung_bay"):
        raise ValueError("scene_override must be Bien_hieu, Trung_bay, or None")
    scene = scene_override or (
        "Bien_hieu" if scores.sign_probability >= 0.5 else "Trung_bay"
    )

    if scores.reference_similarity < policy.reference_similarity_min:
        return ScoringDecision(
            "Can_duyet",
            scene,
            "REVIEW_NOVELTY",
            0.0,
            ("Ảnh ngoài miền tham chiếu",),
        )

    if scores.fraud_probability >= policy.fraud_auto_fail_min:
        return ScoringDecision(
            "Khong_dat",
            scene,
            "AUTO_FAIL_FRAUD",
            scores.fraud_probability,
            ("Bằng chứng đối phó rất mạnh",),
        )

    if scores.fraud_probability >= policy.fraud_review_min:
        return ScoringDecision(
            "Can_duyet",
            scene,
            "REVIEW_FRAUD",
            scores.fraud_probability,
            ("Có tín hiệu đối phó cần duyệt",),
        )

    if (
        scene_override is None
        and abs(scores.sign_probability - 0.5) < policy.scene_margin_min
    ):
        return ScoringDecision(
            "Can_duyet",
            scene,
            "REVIEW_SCENE",
            0.0,
            ("Loại cảnh chưa rõ",),
        )

    if scores.pass_probability >= policy.auto_pass_min:
        return ScoringDecision(
            scene,
            scene,
            "PASS_CANDIDATE",
            scores.pass_probability,
            ("Model đạt ngưỡng pass",),
        )

    if scores.pass_probability <= policy.auto_fail_max:
        return ScoringDecision(
            "Khong_dat",
            scene,
            "AUTO_FAIL_VALIDITY",
            1.0 - scores.pass_probability,
            ("Bằng chứng không đạt rất mạnh",),
        )

    return ScoringDecision(
        "Can_duyet",
        scene,
        "REVIEW_VALIDITY",
        0.0,
        ("Điểm nằm trong vùng duyệt",),
    )


def apply_detector_evidence(
    decision: ScoringDecision,
    evidence: DetectorEvidence,
) -> ScoringDecision:
    """Confirm a pass candidate or send it to review when evidence is absent."""

    if decision.status != "PASS_CANDIDATE":
        return decision

    if decision.scene == "Bien_hieu":
        supported = evidence.has_signboard or evidence.has_brand_keyword
    else:
        supported = evidence.has_bottle_or_pack

    if supported:
        return replace(decision, status="AUTO_PASS")

    return replace(
        decision,
        label="Can_duyet",
        status="REVIEW_MISSING_EVIDENCE",
        reasons=decision.reasons + ("Thiếu bằng chứng detector/OCR",),
    )


def apply_quality_gates(
    decision: ScoringDecision,
    pass_gate_passed: bool,
    auto_fail_gate_passed: bool,
) -> ScoringDecision:
    """Downgrade automatic decisions whose OOF validation gate has not passed."""

    if decision.status == "PASS_CANDIDATE" and not pass_gate_passed:
        reason = "Quality gate auto-pass chưa đủ"
    elif decision.status in {"AUTO_FAIL_VALIDITY", "AUTO_FAIL_FRAUD"} and not auto_fail_gate_passed:
        reason = "Quality gate auto-fail chưa đủ"
    else:
        return decision
    return replace(
        decision,
        label="Can_duyet",
        status="REVIEW_QUALITY_GATE",
        score=0.0,
        reasons=decision.reasons + (reason,),
    )
