"""Runtime classifier for a trusted, prebuilt V2 scoring bundle.

This path is used by ephemeral GitHub-hosted runners. The expensive reference
training set does not need to be downloaded on every run; SharePoint stores a
versioned pickle containing embeddings, trained heads and OOF evaluation.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import pickle
import sys
import types
from dataclasses import asdict, replace
from typing import Sequence

import numpy as np

from scoring.classifier import (
    ClassificationResult,
    NeighborEvidence,
    _implementation_hash,
)
from scoring.config import (
    CACHE_FILE,
    CACHE_SCHEMA_VERSION,
    CLIP_MODEL_ID,
    CLIP_MODEL_REVISION,
    PIPELINE_VERSION,
)
from scoring.decision_policy import (
    DecisionPolicy,
    ScoreVector,
    apply_quality_gates,
    decide_scores,
)
from scoring.modeling import EvaluationReport, score_embeddings


_REQUIRED_FIELDS = {
    "schema_version",
    "pipeline_version",
    "embeddings",
    "effective_subcategories",
    "relative_paths",
    "heads",
    "evaluation_report",
    "model_signature",
    "clip_model_id",
    "clip_revision",
    "thresholds",
    "implementation_hash",
}

# The deployed bundle was built before ``ClassificationResult`` carried the
# auto-fail gate explicitly.  That change affects only the runtime result
# contract; embeddings, trained heads, thresholds and evaluation data remain
# identical.  Keep this one exact hash so the production asset can cross that
# contract upgrade without enabling the broad legacy-bundle escape hatch.
_COMPATIBLE_V23_IMPLEMENTATION_HASHES = frozenset(
    {"bf00cf1d94b0bb1070a88671e95e11f55f998bad9c1c30ca82b6cdcb842d6331"}
)
_RESULT_GATE_CONTRACT_IMPLEMENTATION_HASHES = frozenset(
    {
        # Linux/GitHub-hosted checkout (LF).
        "2589b464d36660e6bc66de6f3fc87cdef6bc3c66b8718fcd19f4bbad4e6ef01e",
        # Windows checkout with core.autocrlf (CRLF).
        "9051d797192f2c6129fcd81ee4181445d5af6f01d2bcd4b175df1db892f5bc9a",
    }
)


def _implementation_is_compatible(
    bundle_hash: str,
    current_hash: str,
    *,
    pipeline_version: str,
    schema_version: int,
) -> bool:
    """Accept the current code or the one audited V2.3 contract-only upgrade."""

    if bundle_hash == current_hash:
        return True
    return (
        pipeline_version == "2.3.0"
        and schema_version == 4
        and bundle_hash in _COMPATIBLE_V23_IMPLEMENTATION_HASHES
        and current_hash in _RESULT_GATE_CONTRACT_IMPLEMENTATION_HASHES
    )


def _install_legacy_pickle_aliases() -> None:
    """Allow legacy project and NumPy-2-produced bundles to unpickle safely."""

    import scoring.decision_policy as decision_policy
    import scoring.modeling as modeling
    import scoring.reference_data as reference_data

    package = sys.modules.get("modules")
    if package is None:
        package = types.ModuleType("modules")
        package.__path__ = []
        sys.modules["modules"] = package
    sys.modules["modules.modeling"] = modeling
    sys.modules["modules.reference_data"] = reference_data
    sys.modules["modules.decision_policy"] = decision_policy

    # The current cloud runtime deliberately pins NumPy 1.26 for broad CV
    # compatibility. The legacy bundle was serialized by NumPy 2.x, which
    # records ``numpy._core.numeric`` in the pickle. NumPy 1.x exposes the same
    # implementation as ``numpy.core.numeric``. Alias only that exact module;
    # no array values or model parameters are transformed.
    if "numpy._core.numeric" not in sys.modules:
        sys.modules["numpy._core.numeric"] = importlib.import_module("numpy.core.numeric")


def _bundle_signature(payload: dict[str, object], policy: DecisionPolicy) -> str:
    digest = hashlib.sha256()
    digest.update(CACHE_FILE.read_bytes())
    digest.update(PIPELINE_VERSION.encode("utf-8"))
    digest.update(
        json.dumps(
            asdict(policy),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    digest.update(b"sharepoint-prebuilt-runtime-v1")
    return digest.hexdigest()


def _legacy_bundle_allowed() -> bool:
    return os.environ.get("AI_ALLOW_LEGACY_PREBUILT_BUNDLE", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


class PrebuiltSceneClassifier:
    """Score images from a trusted SharePoint-hosted reference bundle."""

    def __init__(self, feature_extractor=None, policy: DecisionPolicy | None = None) -> None:
        self.policy = policy or DecisionPolicy()
        self.feature_extractor = feature_extractor
        if not CACHE_FILE.is_file():
            raise FileNotFoundError(f"Prebuilt scoring bundle is missing: {CACHE_FILE}")

        _install_legacy_pickle_aliases()
        with CACHE_FILE.open("rb") as source:
            payload = pickle.load(source)
        if not isinstance(payload, dict) or not _REQUIRED_FIELDS.issubset(payload):
            raise ValueError("Prebuilt scoring bundle is missing required fields")
        if str(payload.get("clip_model_id")) != CLIP_MODEL_ID:
            raise ValueError("Prebuilt bundle CLIP model does not match runtime config")
        if str(payload.get("clip_revision")) != CLIP_MODEL_REVISION:
            raise ValueError("Prebuilt bundle CLIP revision does not match runtime config")
        if dict(payload.get("thresholds") or {}) != asdict(self.policy):
            raise ValueError("Prebuilt bundle decision thresholds do not match runtime policy")

        allow_legacy = _legacy_bundle_allowed()
        bundle_pipeline = str(payload.get("pipeline_version", ""))
        bundle_schema = int(payload.get("schema_version", -1))
        bundle_implementation = str(payload.get("implementation_hash", ""))
        if not allow_legacy and bundle_pipeline != PIPELINE_VERSION:
            raise ValueError(
                "Prebuilt bundle pipeline version does not match runtime: "
                f"bundle={bundle_pipeline!r} runtime={PIPELINE_VERSION!r}"
            )
        if not allow_legacy and bundle_schema != CACHE_SCHEMA_VERSION:
            raise ValueError(
                "Prebuilt bundle schema version does not match runtime: "
                f"bundle={bundle_schema} runtime={CACHE_SCHEMA_VERSION}"
            )
        expected_implementation = _implementation_hash()
        if not allow_legacy and not _implementation_is_compatible(
            bundle_implementation,
            expected_implementation,
            pipeline_version=bundle_pipeline,
            schema_version=bundle_schema,
        ):
            raise ValueError(
                "Prebuilt bundle implementation hash does not match current scoring code"
            )

        self.reference_embeddings = np.asarray(payload["embeddings"], dtype=np.float32)
        self.reference_subcategories = np.asarray(
            payload["effective_subcategories"], dtype=str
        )
        self.reference_paths = tuple(str(value) for value in payload["relative_paths"])
        self.heads = payload["heads"]
        self.evaluation_report: EvaluationReport = payload["evaluation_report"]
        self.grouping_report = dict(payload.get("grouping_report") or {})
        self.quality_gate_passed = self.evaluation_report.quality_gate_passed
        self.auto_fail_gate_passed = self.evaluation_report.auto_fail_gate_passed
        self.model_signature = _bundle_signature(payload, self.policy)

    def _get_feature_extractor(self):
        if self.feature_extractor is None:
            from scoring.feature_extractor import FeatureExtractor

            self.feature_extractor = FeatureExtractor()
        return self.feature_extractor

    def _classify_embedding_batch(self, queries: np.ndarray) -> list[ClassificationResult]:
        if not len(self.reference_embeddings):
            raise RuntimeError("Prebuilt reference bundle contains no embeddings")
        embeddings = np.asarray(queries, dtype=np.float32)
        if embeddings.ndim == 1:
            embeddings = embeddings.reshape(1, -1)
        if embeddings.ndim != 2:
            raise ValueError("CLIP embeddings must be a 2D matrix")
        if not len(embeddings):
            return []

        model_scores = score_embeddings(self.heads, embeddings)
        similarities = embeddings @ self.reference_embeddings.T
        neighbour_count = min(3, similarities.shape[1])
        nearest_indices = np.argsort(similarities, axis=1)[:, -neighbour_count:][:, ::-1]

        results: list[ClassificationResult] = []
        for row_index, indices in enumerate(nearest_indices):
            neighbors = tuple(
                NeighborEvidence(
                    relative_path=self.reference_paths[int(index)],
                    effective_subcategory=self.reference_subcategories[int(index)],
                    similarity=float(similarities[row_index, int(index)]),
                )
                for index in indices
            )
            reference_similarity = float(
                np.mean([neighbor.similarity for neighbor in neighbors])
            )
            scores = ScoreVector(
                sign_probability=float(model_scores.sign_probability[row_index]),
                pass_probability=float(model_scores.pass_probability[row_index]),
                fraud_probability=float(model_scores.fraud_probability[row_index]),
                reference_similarity=reference_similarity,
            )
            decision = apply_quality_gates(
                decide_scores(scores, self.policy),
                self.quality_gate_passed,
                self.auto_fail_gate_passed,
            )
            results.append(
                ClassificationResult(
                    decision=decision,
                    scores=scores,
                    neighbors=neighbors,
                    quality_gate_passed=self.quality_gate_passed,
                    auto_fail_gate_passed=self.auto_fail_gate_passed,
                    sign_pass_probability=float(
                        model_scores.sign_pass_probability[row_index]
                    ),
                    display_pass_probability=float(
                        model_scores.display_pass_probability[row_index]
                    ),
                )
            )
        return results

    def classify_batch(self, images_rgb: Sequence[object]) -> list[ClassificationResult]:
        if not images_rgb:
            return []
        embeddings = self._get_feature_extractor().extract_batch(images_rgb)
        return self._classify_embedding_batch(embeddings)

    def classify(self, image_rgb) -> ClassificationResult:
        return self.classify_batch([image_rgb])[0]

    def resolve_scene(
        self,
        classification: ClassificationResult,
        scene: str,
        reason: str,
    ) -> ClassificationResult:
        if classification.decision.status != "REVIEW_SCENE":
            return classification
        if scene not in {"Bien_hieu", "Trung_bay"}:
            raise ValueError("scene must be Bien_hieu or Trung_bay")

        pass_probability = (
            classification.sign_pass_probability
            if scene == "Bien_hieu"
            else classification.display_pass_probability
        )
        resolved_scores = ScoreVector(
            sign_probability=classification.scores.sign_probability,
            pass_probability=pass_probability,
            fraud_probability=classification.scores.fraud_probability,
            reference_similarity=classification.scores.reference_similarity,
        )
        decision = apply_quality_gates(
            decide_scores(resolved_scores, self.policy, scene_override=scene),
            self.quality_gate_passed,
            self.auto_fail_gate_passed,
        )
        decision = replace(
            decision,
            reasons=(reason,) + tuple(decision.reasons),
        )
        return ClassificationResult(
            decision=decision,
            scores=resolved_scores,
            neighbors=classification.neighbors,
            quality_gate_passed=classification.quality_gate_passed,
            auto_fail_gate_passed=classification.auto_fail_gate_passed,
            sign_pass_probability=classification.sign_pass_probability,
            display_pass_probability=classification.display_pass_probability,
        )
