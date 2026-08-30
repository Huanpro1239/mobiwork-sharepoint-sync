"""Trusted prebuilt classifier bundle for ephemeral cloud runners.

The production bundle is built and validated offline from the private reference
set. Cloud runners fail closed if the bundle identity is incompatible; they do
not silently retrain from missing reference images.
"""
from __future__ import annotations

import pickle
from dataclasses import asdict
from pathlib import Path
from typing import Mapping

import numpy as np
from rich.console import Console

from .classifier import BUNDLE_FIELDS, SceneClassifier
from .config import (
    CACHE_FILE,
    CACHE_SCHEMA_VERSION,
    CLIP_MODEL_ID,
    CLIP_MODEL_REVISION,
    PIPELINE_VERSION,
)
from .decision_policy import DecisionPolicy


console = Console()


class TrustedBundleClassifier(SceneClassifier):
    """Load a SharePoint-hosted V2.3 bundle without requiring reference photos."""

    def __init__(self, feature_extractor=None, policy: DecisionPolicy | None = None):
        self.policy = policy or DecisionPolicy()
        self.feature_extractor = feature_extractor
        bundle = self._load_trusted_bundle(Path(CACHE_FILE))

        self.reference_embeddings = np.asarray(bundle["embeddings"], dtype=np.float32)
        self.reference_subcategories = np.asarray(
            bundle["effective_subcategories"], dtype=str
        )
        self.reference_paths = tuple(str(value) for value in bundle["relative_paths"])
        self.heads = bundle["heads"]
        self.evaluation_report = bundle["evaluation_report"]
        self.grouping_report = dict(bundle["grouping_report"])
        self.model_signature = str(bundle["model_signature"])
        self.quality_gate_passed = self.evaluation_report.quality_gate_passed
        self.auto_fail_gate_passed = self.evaluation_report.auto_fail_gate_passed

        if not len(self.reference_embeddings):
            raise RuntimeError("Trusted model bundle contains no reference embeddings")
        if len(self.reference_paths) != len(self.reference_embeddings):
            raise RuntimeError("Trusted model bundle reference path count is inconsistent")
        console.print(f"[cyan]Nạp trusted model bundle V2.3: {CACHE_FILE}[/cyan]")

    def _load_trusted_bundle(self, path: Path) -> dict[str, object]:
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"Trusted model bundle missing: {path}")
        try:
            with path.open("rb") as source:
                payload = pickle.load(source)
        except Exception as error:
            raise RuntimeError(
                f"Trusted model bundle cannot be loaded: {type(error).__name__}: {error}"
            ) from error

        if not isinstance(payload, Mapping) or not BUNDLE_FIELDS.issubset(payload):
            missing = sorted(BUNDLE_FIELDS - set(payload if isinstance(payload, Mapping) else ()))
            raise RuntimeError(f"Trusted model bundle is incomplete; missing={missing}")

        identity = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "clip_model_id": CLIP_MODEL_ID,
            "clip_revision": CLIP_MODEL_REVISION,
        }
        for key, expected in identity.items():
            if payload.get(key) != expected:
                raise RuntimeError(
                    f"Trusted model bundle identity mismatch for {key}: "
                    f"expected={expected!r} actual={payload.get(key)!r}"
                )

        expected_thresholds = asdict(self.policy)
        if payload.get("thresholds") != expected_thresholds:
            raise RuntimeError("Trusted model bundle thresholds do not match V2.3 policy")
        if not str(payload.get("model_signature", "")).strip():
            raise RuntimeError("Trusted model bundle has no model_signature")
        return dict(payload)
