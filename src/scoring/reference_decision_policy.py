"""Production decision cascade aligned with the human-labelled local reference project.

The trained V2.3 CLIP/logistic bundle remains immutable.  This module consumes the
scores and nearest human-labelled neighbours already produced by that bundle, then
combines them with lightweight detector/OCR evidence.  Detector/OCR can support a
learned decision but can never manufacture a pass on their own.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from scoring.decision_policy import DetectorEvidence, ScoreVector, ScoringDecision


# Defaults copied from the validated local project's tiered policy.  They live in
# this production-only module so changing them does not invalidate the trained
# V2.3 bundle implementation hash.
TIER_FRAUD_AUTO_FAIL_MIN = 0.85
TIER_FRAUD_NEIGHBOR_AUTO_FAIL_MIN = 0.70
TIER_HIGH_PASS_MIN = 0.80
TIER_HIGH_PASS_FRAUD_MAX = 0.35
TIER_EVIDENCE_PASS_MIN = 0.45
TIER_EVIDENCE_PASS_FRAUD_MAX = 0.50
TIER_CONSENSUS_SIM_MIN = 0.75
TIER_CLEAR_FAIL_MAX = 0.30
TIER_WEIGHTED_PASS_MIN = 0.40
TIER_WEIGHTED_REVIEW_MARGIN = 0.10
REFERENCE_SIMILARITY_MIN = 0.70
FRAUD_REVIEW_MIN = 0.60

_AUTO_PASS_STATUSES = frozenset(
    {
        "TIER1_HIGH_PASS",
        "TIER2_EVIDENCE_PASS",
        "TIER2_CONSENSUS_PASS",
        "TIER4_WEIGHTED_PASS",
    }
)
_AUTO_FAIL_STATUSES = frozenset(
    {
        "TIER0_AUTO_FAIL_FRAUD",
        "TIER3_CLEAR_FAIL",
        "TIER4_WEIGHTED_FAIL",
    }
)


@dataclass(frozen=True)
class ReferenceTierFeatures:
    nn_mean_similarity: float
    has_doi_pho_neighbor: bool
    nn_sign_pass: bool
    nn_display_pass: bool
    nn_all_fail: bool
    has_sign_evidence: bool
    has_display_evidence: bool


def _normalise_subcategory(value: object) -> str:
    text = str(value or "").casefold().replace("\\", "/").replace("_", " ")
    return " ".join(text.split())


def _derive_features(
    scores: ScoreVector,
    evidence: DetectorEvidence,
    neighbors: tuple,
    *,
    store_keyword: bool,
) -> ReferenceTierFeatures:
    subcategories = [
        _normalise_subcategory(getattr(neighbor, "effective_subcategory", ""))
        for neighbor in (neighbors or ())
    ]
    similarities = [
        float(getattr(neighbor, "similarity", 0.0))
        for neighbor in (neighbors or ())
    ]
    mean_similarity = (
        float(sum(similarities) / len(similarities))
        if similarities
        else float(scores.reference_similarity)
    )
    first_two = subcategories[:2]
    return ReferenceTierFeatures(
        nn_mean_similarity=mean_similarity,
        has_doi_pho_neighbor=any("doi pho" in value for value in subcategories),
        nn_sign_pass=(
            len(first_two) == 2
            and all(value.startswith("dat/bien hieu") for value in first_two)
        ),
        nn_display_pass=(
            len(first_two) == 2
            and all(value.startswith("dat/trung bay") for value in first_two)
        ),
        nn_all_fail=(
            len(first_two) == 2
            and all(value.startswith("khong dat") for value in first_two)
        ),
        # Human-labelled good sign examples include ordinary store signs.  Brand
        # text is useful but is not a business requirement for a valid sign photo.
        has_sign_evidence=bool(
            evidence.has_signboard or evidence.has_brand_keyword or store_keyword
        ),
        # Product detection is only one supporting feature.  A close-up carton or
        # a bottle held in hand can still be human-labelled invalid/doi-pho.
        has_display_evidence=bool(evidence.has_bottle_or_pack),
    )


def neighbor_scene_consensus(neighbors: tuple) -> str | None:
    """Return a scene only when the two nearest human references agree on scene."""

    values = [
        _normalise_subcategory(getattr(neighbor, "effective_subcategory", ""))
        for neighbor in (neighbors or ())[:2]
    ]
    if len(values) != 2:
        return None
    if all("bien hieu" in value for value in values):
        return "Bien_hieu"
    if all("trung bay" in value for value in values):
        return "Trung_bay"
    return None


def _apply_quality_gates(
    decision: ScoringDecision,
    *,
    pass_gate_passed: bool,
    auto_fail_gate_passed: bool,
) -> ScoringDecision:
    if decision.status in _AUTO_PASS_STATUSES and not pass_gate_passed:
        reason = "Quality gate OOF cho auto-pass chưa đạt"
    elif decision.status in _AUTO_FAIL_STATUSES and not auto_fail_gate_passed:
        reason = "Quality gate OOF cho auto-fail chưa đạt"
    else:
        return decision
    return replace(
        decision,
        label="Can_duyet",
        status="REVIEW_QUALITY_GATE",
        score=0.0,
        reasons=decision.reasons + (reason,),
    )


def decide_reference_tiered_scores(
    scores: ScoreVector,
    evidence: DetectorEvidence,
    neighbors: tuple = (),
    *,
    store_keyword: bool = False,
    pass_gate_passed: bool = True,
    auto_fail_gate_passed: bool = True,
    scene_override: str | None = None,
    scene_ambiguous: bool = False,
) -> ScoringDecision:
    """Resolve one image using learned heads, human-reference consensus and evidence.

    Order is intentionally conservative:
      0. anti-gaming/fraud,
      1. out-of-domain novelty,
      2. unresolved scene ambiguity,
      3. high-confidence learned pass,
      4. moderate learned pass backed by evidence or human-reference consensus,
      5. clear learned failure,
      6. weighted multimodal resolution with an explicit review band.

    Every automatic outcome is still subject to the model bundle's OOF quality
    gates.  Physical detection alone is never sufficient because all pass tiers
    require a non-trivial learned pass score.
    """

    if scene_override not in {None, "Bien_hieu", "Trung_bay"}:
        raise ValueError("scene_override must be Bien_hieu, Trung_bay, or None")
    scene = scene_override or (
        "Bien_hieu" if float(scores.sign_probability) >= 0.5 else "Trung_bay"
    )
    features = _derive_features(
        scores,
        evidence,
        neighbors,
        store_keyword=store_keyword,
    )

    def final(decision: ScoringDecision) -> ScoringDecision:
        return _apply_quality_gates(
            decision,
            pass_gate_passed=pass_gate_passed,
            auto_fail_gate_passed=auto_fail_gate_passed,
        )

    # Tier 0: strong anti-gaming signal.  A close human reference in the doi-pho
    # class lowers the fraud threshold, matching the local project's behaviour.
    if float(scores.fraud_probability) >= TIER_FRAUD_AUTO_FAIL_MIN or (
        float(scores.fraud_probability) >= TIER_FRAUD_NEIGHBOR_AUTO_FAIL_MIN
        and features.has_doi_pho_neighbor
    ):
        return final(
            ScoringDecision(
                "Khong_dat",
                scene,
                "TIER0_AUTO_FAIL_FRAUD",
                float(scores.fraud_probability),
                ("Bằng chứng đối phó/gian lận rất mạnh",),
            )
        )
    if float(scores.fraud_probability) >= FRAUD_REVIEW_MIN:
        return ScoringDecision(
            "Can_duyet",
            scene,
            "TIER0_REVIEW_FRAUD",
            float(scores.fraud_probability),
            ("Có tín hiệu đối phó cần duyệt",),
        )

    # Novel visual patterns must be reviewed instead of extrapolated.
    if float(scores.reference_similarity) < REFERENCE_SIMILARITY_MIN:
        return ScoringDecision(
            "Can_duyet",
            scene,
            "REVIEW_NOVELTY",
            0.0,
            ("Ảnh ngoài miền tham chiếu người chấm",),
        )

    if scene_ambiguous:
        return ScoringDecision(
            "Can_duyet",
            scene,
            "REVIEW_SCENE",
            0.0,
            ("Loại cảnh chưa đủ rõ sau detector/OCR/reference consensus",),
        )

    pass_probability = float(scores.pass_probability)
    fraud_probability = float(scores.fraud_probability)

    # Tier 1: the learned model is already confident; detector or human-reference
    # consensus only confirms that the image depicts the expected business scene.
    if (
        pass_probability >= TIER_HIGH_PASS_MIN
        and fraud_probability < TIER_HIGH_PASS_FRAUD_MAX
    ):
        supported = (
            (scene == "Bien_hieu" and (features.has_sign_evidence or features.nn_sign_pass))
            or (
                scene == "Trung_bay"
                and (features.has_display_evidence or features.nn_display_pass)
            )
        )
        if supported:
            return final(
                ScoringDecision(
                    scene,
                    scene,
                    "TIER1_HIGH_PASS",
                    pass_probability,
                    ("Model học từ ảnh người chấm đạt ngưỡng cao + có xác nhận cảnh",),
                )
            )

    # Tier 2: moderate model confidence is allowed only when backed by either
    # physical scene evidence or two close human-labelled positive neighbours.
    if (
        pass_probability >= TIER_EVIDENCE_PASS_MIN
        and fraud_probability < TIER_EVIDENCE_PASS_FRAUD_MAX
    ):
        if scene == "Bien_hieu":
            if features.has_sign_evidence:
                return final(
                    ScoringDecision(
                        "Bien_hieu",
                        "Bien_hieu",
                        "TIER2_EVIDENCE_PASS",
                        pass_probability,
                        ("Model + bằng chứng biển/tên cửa hàng xác nhận",),
                    )
                )
            if (
                features.nn_sign_pass
                and features.nn_mean_similarity >= TIER_CONSENSUS_SIM_MIN
            ):
                return final(
                    ScoringDecision(
                        "Bien_hieu",
                        "Bien_hieu",
                        "TIER2_CONSENSUS_PASS",
                        pass_probability,
                        ("Hai mẫu người chấm gần nhất đồng thuận Biển hiệu đạt",),
                    )
                )
        else:
            if features.has_display_evidence:
                return final(
                    ScoringDecision(
                        "Trung_bay",
                        "Trung_bay",
                        "TIER2_EVIDENCE_PASS",
                        pass_probability,
                        ("Model + bằng chứng sản phẩm trong cảnh trưng bày xác nhận",),
                    )
                )
            if (
                features.nn_display_pass
                and features.nn_mean_similarity >= TIER_CONSENSUS_SIM_MIN
            ):
                return final(
                    ScoringDecision(
                        "Trung_bay",
                        "Trung_bay",
                        "TIER2_CONSENSUS_PASS",
                        pass_probability,
                        ("Hai mẫu người chấm gần nhất đồng thuận Trưng bày đạt",),
                    )
                )

    relevant_evidence = (
        features.has_sign_evidence if scene == "Bien_hieu" else features.has_display_evidence
    )
    if pass_probability <= TIER_CLEAR_FAIL_MAX and (
        features.nn_all_fail or not relevant_evidence
    ):
        return final(
            ScoringDecision(
                "Khong_dat",
                scene,
                "TIER3_CLEAR_FAIL",
                1.0 - pass_probability,
                ("Điểm đạt thấp và ảnh người chấm/bằng chứng cảnh không ủng hộ",),
            )
        )

    evidence_score = 0.25 if relevant_evidence else 0.0
    positive_consensus = (
        features.nn_sign_pass if scene == "Bien_hieu" else features.nn_display_pass
    )
    consensus_score = 0.25 if positive_consensus else (-0.25 if features.nn_all_fail else 0.0)
    weighted_score = (
        0.50 * pass_probability
        + evidence_score
        + consensus_score
        - 0.30 * fraud_probability
    )
    upper = TIER_WEIGHTED_PASS_MIN + TIER_WEIGHTED_REVIEW_MARGIN
    lower = TIER_WEIGHTED_PASS_MIN - TIER_WEIGHTED_REVIEW_MARGIN

    if weighted_score >= upper:
        return final(
            ScoringDecision(
                scene,
                scene,
                "TIER4_WEIGHTED_PASS",
                pass_probability,
                ("Phân giải tổng hợp model + ảnh người chấm + evidence: Đạt",),
            )
        )
    if weighted_score <= lower:
        return final(
            ScoringDecision(
                "Khong_dat",
                scene,
                "TIER4_WEIGHTED_FAIL",
                1.0 - pass_probability,
                ("Phân giải tổng hợp model + ảnh người chấm + evidence: Không đạt",),
            )
        )
    return ScoringDecision(
        "Can_duyet",
        scene,
        "TIER4_WEIGHTED_REVIEW",
        0.0,
        ("Ảnh nằm trong vùng mơ hồ; cần người duyệt để tiếp tục học",),
    )
