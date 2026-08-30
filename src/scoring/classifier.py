"""Versioned CLIP feature bundle and conservative scene classification."""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

import numpy as np
from PIL import Image
from rich.console import Console

from .config import (
    CACHE_FILE,
    CACHE_SCHEMA_VERSION,
    CLIP_INFERENCE_BATCH_SIZE,
    CLIP_MODEL_ID,
    CLIP_MODEL_REVISION,
    MODEL_CV_FOLDS,
    PIPELINE_VERSION,
    REFERENCE_CATEGORIES,
    REFERENCE_DIR,
    REFERENCE_OVERRIDES,
    VISUAL_CONFLICT_SIMILARITY,
)
from .decision_policy import (
    DecisionPolicy,
    ScoreVector,
    ScoringDecision,
    apply_quality_gates,
    decide_scores,
)
if TYPE_CHECKING:
    from .feature_extractor import FeatureExtractor
from .modeling import (
    EvaluationReport,
    cross_validate_heads,
    score_embeddings,
    train_heads,
)
from .reference_data import (
    ReferenceRecord,
    collect_reference_records,
    find_visual_conflicts,
    reference_fingerprint,
    sha256_file,
)


console = Console()
CUSTOMER_CODE_PATTERN = re.compile(
    r"(?<![A-Z0-9])([A-Z]{4}\d{6,7}|KH\d{5})(?!\d)"
)
ROUTE_CODE_PATTERN = re.compile(r"(?<![A-Z0-9])([A-Z]{4}\d{4})(?!\d)")
BUNDLE_FIELDS = frozenset(
    {
        "embeddings",
        "effective_subcategories",
        "relative_paths",
        "heads",
        "evaluation_report",
        "grouping_report",
        "visual_conflicts",
        "model_signature",
    }
)


@dataclass(frozen=True)
class NeighborEvidence:
    relative_path: str
    effective_subcategory: str
    similarity: float


@dataclass(frozen=True)
class ClassificationResult:
    decision: ScoringDecision
    scores: ScoreVector
    neighbors: tuple[NeighborEvidence, ...]
    quality_gate_passed: bool
    sign_pass_probability: float
    display_pass_probability: float



def cache_metadata_matches(
    cached: Mapping[str, object] | object,
    expected: Mapping[str, object],
) -> bool:
    """Return true only when every expected metadata value matches exactly."""

    if not isinstance(cached, Mapping) or not isinstance(expected, Mapping):
        return False
    return all(key in cached and cached[key] == value for key, value in expected.items())


def _json_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _implementation_hash() -> str:
    source_paths = (
        Path(__file__).with_name("config.py"),
        Path(__file__).with_name("decision_policy.py"),
        Path(__file__).with_name("reference_data.py"),
        Path(__file__).with_name("modeling.py"),
        Path(__file__).with_name("feature_extractor.py"),
        Path(__file__).with_name("classifier.py"),
    )
    digest = hashlib.sha256()
    for path in source_paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _customer_group(relative_path: str) -> str:
    filename = Path(relative_path).name.upper()
    match = CUSTOMER_CODE_PATTERN.search(filename)
    if match:
        return f"CUSTOMER::{match.group(1)}"
    route_match = ROUTE_CODE_PATTERN.search(filename)
    if route_match:
        return f"ROUTE::{route_match.group(1)}"
    # Opaque uploads cannot safely be claimed as independent customers.  Keep
    # them in one quarantine group so they never leak across OOF folds.
    return "UNRESOLVED"


def _grouping_report(groups: Sequence[str]) -> dict[str, float | int]:
    values = np.asarray(groups, dtype=str)
    total = int(values.size)
    customer_rows = int(np.count_nonzero(np.char.startswith(values, "CUSTOMER::")))
    route_rows = int(np.count_nonzero(np.char.startswith(values, "ROUTE::")))
    unresolved_rows = int(np.count_nonzero(values == "UNRESOLVED"))
    return {
        "total_rows": total,
        "unique_groups": int(np.unique(values).size),
        "customer_rows": customer_rows,
        "route_rows": route_rows,
        "unresolved_rows": unresolved_rows,
        "unresolved_fraction": (unresolved_rows / total) if total else 0.0,
    }


def _expected_bundle_metadata(
    records: Sequence[ReferenceRecord],
    policy: DecisionPolicy,
) -> dict[str, object]:
    thresholds = asdict(policy)
    registry_hash = sha256_file(REFERENCE_OVERRIDES)
    policy_hash = _json_hash(thresholds)
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "dataset_fingerprint": reference_fingerprint(
            records,
            registry_hash,
            CLIP_MODEL_REVISION,
        ),
        "registry_hash": registry_hash,
        "policy_hash": policy_hash,
        "thresholds": thresholds,
        "clip_model_id": CLIP_MODEL_ID,
        "clip_revision": CLIP_MODEL_REVISION,
        "implementation_hash": _implementation_hash(),
    }


def _load_rgb_images(records: Sequence[ReferenceRecord]) -> list[Image.Image]:
    images: list[Image.Image] = []
    for record in records:
        try:
            with Image.open(record.path) as source:
                images.append(source.convert("RGB").copy())
        except Exception as error:
            raise ValueError(
                f"Invalid reference image {record.relative_path}: "
                f"{type(error).__name__}: {error}"
            ) from error
    return images


def _extract_embeddings(
    extractor: FeatureExtractor,
    records: Sequence[ReferenceRecord],
    batch_size: int = 64,
) -> np.ndarray:
    batches: list[np.ndarray] = []
    for start in range(0, len(records), batch_size):
        batch_records = records[start : start + batch_size]
        images = _load_rgb_images(batch_records)
        try:
            batches.append(extractor.extract_batch(images, batch_size=batch_size))
        finally:
            for image in images:
                image.close()
        console.print(
            f"[cyan]CLIP references: {min(start + batch_size, len(records))}/"
            f"{len(records)}[/cyan]"
        )
    if not batches:
        raise ValueError("No usable reference images were found")
    return np.vstack(batches).astype(np.float32, copy=False)


def _save_bundle(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as destination:
        pickle.dump(dict(payload), destination, protocol=pickle.HIGHEST_PROTOCOL)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary, path)


class SceneClassifier:
    """Score one RGB image using a validated, versioned reference bundle."""

    def __init__(
        self,
        feature_extractor: FeatureExtractor | None = None,
        policy: DecisionPolicy | None = None,
    ):
        self.policy = policy or DecisionPolicy()
        self.feature_extractor = feature_extractor
        all_records = collect_reference_records(
            REFERENCE_DIR,
            REFERENCE_CATEGORIES,
            REFERENCE_OVERRIDES,
        )
        expected_metadata = _expected_bundle_metadata(all_records, self.policy)
        bundle = self._load_current_bundle(expected_metadata)
        if bundle is None:
            bundle = self._build_bundle(all_records, expected_metadata)
            _save_bundle(CACHE_FILE, bundle)
            console.print(f"[bold green]Đaã lưu model bundle v2: {CACHE_FILE}[/bold green]")

        self.reference_embeddings = np.asarray(bundle["embeddings"], dtype=np.float32)
        self.reference_subcategories = np.asarray(
            bundle["effective_subcategories"], dtype=str
        )
        self.reference_paths = tuple(str(value) for value in bundle["relative_paths"])
        self.heads = bundle["heads"]
        self.evaluation_report: EvaluationReport = bundle["evaluation_report"]
        self.grouping_report = dict(bundle["grouping_report"])
        self.model_signature = str(bundle["model_signature"])
        self.quality_gate_passed = self.evaluation_report.quality_gate_passed
        self.auto_fail_gate_passed = self.evaluation_report.auto_fail_gate_passed

    def _load_current_bundle(
        self,
        expected_metadata: Mapping[str, object],
    ) -> dict[str, object] | None:
        if not CACHE_FILE.is_file():
            return None
        try:
            with CACHE_FILE.open("rb") as source:
                payload = pickle.load(source)
        except Exception as error:
            console.print(
                f"[yellow]Không dụng được cache v2 ({type(error).__name__}: {error}); "
                "sẽ build lại.[yellow]"
            )
            return None
        if not cache_metadata_matches(payload, expected_metadata):
            console.print("[yellow]Cache v2 đã cũ; sẽ build lại an toàn.[yellow]")
            return None
        if not isinstance(payload, Mapping) or not BUNDLE_FIELDS.issubset(payload):
            console.print("[yellow]Cache v2 thiếu dữ liệu bắt buộc; sẽ build lại.[/yellow]")
            return None
        console.print(f"[cyan]Nạp model bundle v2: {CACHE_FILE}[/cyan]")
        return dict(payload)

    def _get_feature_extractor(self):
        if self.feature_extractor is None:
            from .feature_extractor import FeatureExtractor

            self.feature_extractor = FeatureExtractor()
        return self.feature_extractor

    def _build_bundle(
        self,
        all_records: Sequence[ReferenceRecord],
        metadata: Mapping[str, object],
    ) -> dict[str, object]:
        explicit_exclusions = tuple(
            record.relative_path for record in all_records if record.action == "exclude"
        )
        candidates = [record for record in all_records if record.action != "exclude"]
        relabel_count = sum(record.action == "relabel" for record in candidates)
        console.print(
            "[bold yellow]Build model bundle v2: "
            f"{len(candidates)} ảnh, {relabel_count} relabel, "
            f"{len(explicit_exclusions)} exclude.[/bold yellow]"
        )
        candidate_embeddings = _extract_embeddings(self._get_feature_extractor(), candidates)
        visual_conflicts = find_visual_conflicts(
            candidate_embeddings,
            candidates,
            threshold=VISUAL_CONFLICT_SIMILARITY,
        )
        keep_mask = np.ones(len(candidates), dtype=bool)
        if visual_conflicts.excluded_indices:
            keep_mask[list(visual_conflicts.excluded_indices)] = False
        train_records = [
            record for index, record in enumerate(candidates) if keep_mask[index]
        ]
        embeddings = candidate_embeddings[keep_mask]
        subcategories = np.asarray(
            [record.effective_subcategory for record in train_records],
            dtype=str,
        )
        groups = np.asarray(
            [_customer_group(record.relative_path) for record in train_records],
            dtype=str,
        )
        grouping_report = _grouping_report(groups)
        evaluation = cross_validate_heads(
            embeddings,
            subcategories,
            groups,
            folds=MODEL_CV_FOLDS,
            policy=self.policy,
        )
        heads = train_heads(embeddings, subcategories)
        signature_payload = {
            **dict(metadata),
            "training_rows": len(train_records),
            "relative_paths": [record.relative_path for record in train_records],
            "effective_subcategories": subcategories.tolist(),
            "evaluation": evaluation.to_dict(),
            "grouping_report": grouping_report,
        }
        model_signature = _json_hash(signature_payload)
        console.print(
            "[bold]OOF: "
            f"coverage={evaluation.coverage:.2%}, "
            f"auto-pass precision={evaluation.auto_pass_precision:.2%}, "
            f"pass_gate={evaluation.quality_gate_passed}, "
            f"fail_gate={evaluation.auto_fail_gate_passed}[/bold]"
        )
        return {
            **dict(metadata),
            "embeddings": embeddings,
            "effective_subcategories": subcategories,
            "relative_paths": tuple(record.relative_path for record in train_records),
            "groups": groups,
            "grouping_report": grouping_report,
            "heads": heads,
            "evaluation_report": evaluation,
            "visual_conflicts": visual_conflicts,
            "explicit_exclusions": explicit_exclusions,
            "model_signature": model_signature,
        }

    def _classify_embedding_batch(self, queries: np.ndarray) -> list[ClassificationResult]:
        if not len(self.reference_embeddings):
            raise RuntimeError("Reference bundle contains no training embeddings")

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
                    sign_pass_probability=float(
                        model_scores.sign_pass_probability[row_index]
                    ),
                    display_pass_probability=float(
                        model_scores.display_pass_probability[row_index]
                    ),
                )
            )
        return results

    def resolve_scene(
        self,
        classification: ClassificationResult,
        scene: str,
        reason: str,
    ) -> ClassificationResult:
        """Resolve only an ambiguous scene, then re-run the normal policy.

        Detector/OCR evidence may select the sign or display validity head, but
        the selected head must still pass the original novelty, fraud, validity,
        and model quality gates.
        """

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
            sign_pass_probability=classification.sign_pass_probability,
            display_pass_probability=classification.display_pass_probability,
        )

    def classify_batch(
        self,
        images_rgb: Sequence[np.ndarray | Image.Image],
        batch_size: int = CLIP_INFERENCE_BATCH_SIZE,
    ) -> list[ClassificationResult]:
        """Score multiple RGB images with one batched CLIP forward pass per chunk."""

        if not images_rgb:
            return []
        embeddings = self._get_feature_extractor().extract_batch(
            images_rgb,
            batch_size=batch_size,
        )
        return self._classify_embedding_batch(embeddings)

    def classify(self, image_rgb: np.ndarray | Image.Image) -> ClassificationResult:
        """Backward-compatible single-image API backed by the V2.3 batch path."""

        return self.classify_batch([image_rgb], batch_size=1)[0]
