"""One-time safe migration of a legacy prebuilt bundle to the current model heads.

The CLIP encoder is frozen to an exact model revision, so existing reference
embeddings remain valid when the reference paths/effective labels are unchanged.
This utility does not relabel a legacy bundle as current. It recomputes customer
groups, group-aware OOF evaluation, trained heads, implementation metadata and
the model signature with the current V2.3 code before writing a new bundle.
"""
from __future__ import annotations

import argparse
import pickle
from dataclasses import asdict
from pathlib import Path
from typing import Mapping

import numpy as np

from scoring.classifier import (
    _customer_group,
    _grouping_report,
    _implementation_hash,
    _json_hash,
    _save_bundle,
)
from scoring.config import (
    CACHE_SCHEMA_VERSION,
    CLIP_MODEL_ID,
    CLIP_MODEL_REVISION,
    MODEL_CV_FOLDS,
    PIPELINE_VERSION,
)
from scoring.decision_policy import DecisionPolicy
from scoring.modeling import cross_validate_heads, train_heads
from scoring.prebuilt_classifier import _install_legacy_pickle_aliases


def migrate(source_path: Path, output_path: Path) -> dict[str, object]:
    _install_legacy_pickle_aliases()
    with source_path.open("rb") as source:
        legacy = pickle.load(source)
    if not isinstance(legacy, Mapping):
        raise ValueError("Legacy bundle must be a mapping")

    required = {
        "embeddings",
        "effective_subcategories",
        "relative_paths",
        "clip_model_id",
        "clip_revision",
        "dataset_fingerprint",
    }
    missing = sorted(required.difference(legacy))
    if missing:
        raise ValueError(f"Legacy bundle missing fields: {', '.join(missing)}")
    if str(legacy["clip_model_id"]) != CLIP_MODEL_ID:
        raise ValueError("Legacy bundle CLIP model does not match current runtime")
    if str(legacy["clip_revision"]) != CLIP_MODEL_REVISION:
        raise ValueError("Legacy bundle CLIP revision does not match current runtime")

    embeddings = np.asarray(legacy["embeddings"], dtype=np.float32)
    subcategories = np.asarray(legacy["effective_subcategories"], dtype=str)
    relative_paths = tuple(str(value) for value in legacy["relative_paths"])
    if embeddings.ndim != 2 or embeddings.shape[0] != len(relative_paths):
        raise ValueError("Legacy embeddings/path dimensions are inconsistent")
    if len(subcategories) != len(relative_paths):
        raise ValueError("Legacy labels/path dimensions are inconsistent")
    if not len(relative_paths):
        raise ValueError("Legacy bundle contains no training rows")

    policy = DecisionPolicy()
    thresholds = asdict(policy)
    groups = np.asarray([_customer_group(path) for path in relative_paths], dtype=str)
    grouping_report = _grouping_report(groups)
    evaluation = cross_validate_heads(
        embeddings,
        subcategories,
        groups,
        folds=MODEL_CV_FOLDS,
        policy=policy,
    )
    if not evaluation.quality_gate_passed:
        raise RuntimeError(
            "Migrated V2.3 bundle failed the auto-pass quality gate: "
            f"precision={evaluation.auto_pass_precision:.6f} "
            f"coverage={evaluation.auto_pass_coverage:.6f}"
        )
    if not evaluation.auto_fail_gate_passed:
        raise RuntimeError(
            "Migrated V2.3 bundle failed the auto-fail quality gate: "
            f"precision={evaluation.auto_fail_precision:.6f} "
            f"count={evaluation.auto_fail_count}"
        )

    heads = train_heads(embeddings, subcategories)
    metadata = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        # The image bytes/CLIP revision did not change during this migration.
        "dataset_fingerprint": str(legacy["dataset_fingerprint"]),
        # Preserve the registry identity that produced the effective labels.
        "registry_hash": str(legacy.get("registry_hash", "")),
        "policy_hash": _json_hash(thresholds),
        "thresholds": thresholds,
        "clip_model_id": CLIP_MODEL_ID,
        "clip_revision": CLIP_MODEL_REVISION,
        "implementation_hash": _implementation_hash(),
    }
    signature_payload = {
        **metadata,
        "training_rows": len(relative_paths),
        "relative_paths": list(relative_paths),
        "effective_subcategories": subcategories.tolist(),
        "evaluation": evaluation.to_dict(),
        "grouping_report": grouping_report,
    }
    model_signature = _json_hash(signature_payload)
    payload = {
        **metadata,
        "embeddings": embeddings,
        "effective_subcategories": subcategories,
        "relative_paths": relative_paths,
        "groups": groups,
        "grouping_report": grouping_report,
        "heads": heads,
        "evaluation_report": evaluation,
        "visual_conflicts": legacy.get("visual_conflicts"),
        "explicit_exclusions": tuple(legacy.get("explicit_exclusions") or ()),
        "model_signature": model_signature,
        "migration_provenance": {
            "source_pipeline_version": str(legacy.get("pipeline_version", "unknown")),
            "source_schema_version": legacy.get("schema_version"),
            "source_model_signature": str(legacy.get("model_signature", "")),
            "method": "retrain-current-heads-from-frozen-clip-embeddings",
        },
    }
    _save_bundle(output_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild current V2.3 model heads from a trusted legacy CLIP bundle"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = migrate(args.source, args.output)
    report = payload["evaluation_report"]
    print(f"pipeline_version={payload['pipeline_version']}")
    print(f"schema_version={payload['schema_version']}")
    print(f"training_rows={len(payload['relative_paths'])}")
    print(f"auto_pass_precision={report.auto_pass_precision:.6f}")
    print(f"auto_pass_coverage={report.auto_pass_coverage:.6f}")
    print(f"auto_fail_precision={report.auto_fail_precision:.6f}")
    print(f"auto_fail_count={report.auto_fail_count}")
    print(f"model_signature={payload['model_signature']}")
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
