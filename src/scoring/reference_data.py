"""Reference-dataset registry, fingerprinting, and conflict detection."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

import numpy as np


IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})
VALID_SUBCATEGORIES = frozenset(
    {
        "dat/bien hieu",
        "dat/trung bay",
        "khong dat/khong dat bien hieu",
        "khong dat/khong dat trung bay",
        "khong dat/doi pho",
    }
)
REGISTRY_FIELDS = frozenset(
    {"relative_path", "action", "effective_subcategory", "reason", "source"}
)


@dataclass(frozen=True)
class ReferenceRecord:
    path: Path
    relative_path: str
    source_subcategory: str
    effective_subcategory: str
    action: str


@dataclass(frozen=True)
class ReferenceOverride:
    relative_path: str
    action: str
    effective_subcategory: str
    reason: str
    source: str


@dataclass(frozen=True)
class VisualConflictReport:
    excluded_indices: frozenset[int]
    pairs: tuple[tuple[int, int, float], ...]


def _normalise_relative_path(value: str) -> str:
    raw = value.strip().replace("\\", "/")
    posix_path = PurePosixPath(raw)
    if not raw or posix_path.is_absolute() or ".." in posix_path.parts:
        raise ValueError(f"Registry path must stay below the reference root: {value!r}")
    if any(part in {"", "."} for part in posix_path.parts):
        raise ValueError(f"Invalid registry path: {value!r}")
    return posix_path.as_posix()


def _resolve_below_root(root: Path, relative_path: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / Path(*PurePosixPath(relative_path).parts)).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(
            f"Registry path resolves outside the reference root: {relative_path!r}"
        ) from error
    return candidate


def load_reference_overrides(
    root: Path,
    registry_path: Path,
) -> dict[str, ReferenceOverride]:
    """Parse the override CSV and reject ambiguous or unsafe entries."""

    registry_path = Path(registry_path)
    if not registry_path.is_file():
        raise FileNotFoundError(f"Reference override registry not found: {registry_path}")

    overrides: dict[str, ReferenceOverride] = {}
    with registry_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        fields = set(reader.fieldnames or ())
        if not REGISTRY_FIELDS.issubset(fields):
            missing = ", ".join(sorted(REGISTRY_FIELDS - fields))
            raise ValueError(f"Reference registry is missing columns: {missing}")

        for line_number, row in enumerate(reader, start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            relative_path = _normalise_relative_path(row["relative_path"] or "")
            _resolve_below_root(root, relative_path)
            key = relative_path.casefold()
            if key in overrides:
                raise ValueError(
                    f"Duplicate reference registry path at line {line_number}: "
                    f"{relative_path}"
                )

            action = (row["action"] or "").strip().casefold()
            if action not in {"relabel", "exclude"}:
                raise ValueError(
                    f"Unknown reference action at line {line_number}: {action!r}"
                )

            effective = (row["effective_subcategory"] or "").strip().casefold()
            if action == "relabel" and not effective:
                raise ValueError(
                    f"Relabel action requires effective_subcategory at line {line_number}"
                )
            if effective and effective not in VALID_SUBCATEGORIES:
                raise ValueError(
                    f"Unknown effective_subcategory at line {line_number}: {effective!r}"
                )

            overrides[key] = ReferenceOverride(
                relative_path=relative_path,
                action=action,
                effective_subcategory=effective,
                reason=(row["reason"] or "").strip(),
                source=(row["source"] or "").strip(),
            )
    return overrides


def collect_reference_records(
    root: Path,
    categories: Mapping[str, str],
    registry_path: Path,
) -> list[ReferenceRecord]:
    """Collect source images and apply logical overrides without moving files."""

    root = Path(root).resolve()
    overrides = load_reference_overrides(root, Path(registry_path))
    records: list[ReferenceRecord] = []
    seen_override_keys: set[str] = set()

    for folder_relative in categories:
        folder_posix = _normalise_relative_path(str(folder_relative))
        folder = _resolve_below_root(root, folder_posix)
        if not folder.is_dir():
            raise FileNotFoundError(f"Missing reference category: {folder}")
        source_subcategory = folder_posix.casefold()
        if source_subcategory not in VALID_SUBCATEGORIES:
            raise ValueError(f"Unknown reference category: {folder_relative!r}")

        for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_file() or path.suffix.casefold() not in IMAGE_EXTENSIONS:
                continue
            relative_path = f"{folder_posix}/{path.name}"
            key = relative_path.casefold()
            override = overrides.get(key)
            if override is None:
                action = "keep"
                effective = source_subcategory
            else:
                seen_override_keys.add(key)
                action = override.action
                effective = override.effective_subcategory or source_subcategory
            records.append(
                ReferenceRecord(
                    path=path.resolve(),
                    relative_path=relative_path,
                    source_subcategory=source_subcategory,
                    effective_subcategory=effective,
                    action=action,
                )
            )

    unknown_paths = set(overrides) - seen_override_keys
    if unknown_paths:
        joined = ", ".join(overrides[key].relative_path for key in sorted(unknown_paths))
        raise ValueError(f"Registry paths do not match a reference image: {joined}")
    return records


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reference_fingerprint(
    records: Sequence[ReferenceRecord],
    policy_hash: str,
    clip_revision: str,
) -> str:
    """Fingerprint all reference metadata that determines a model bundle."""

    digest = hashlib.sha256()
    digest.update(policy_hash.encode("utf-8"))
    digest.update(b"\0")
    digest.update(clip_revision.encode("utf-8"))
    for record in sorted(records, key=lambda item: item.relative_path.casefold()):
        stat = record.path.stat()
        payload = (
            f"{record.relative_path}\0{stat.st_size}\0{stat.st_mtime_ns}\0"
            f"{record.effective_subcategory}\0{record.action}"
        )
        digest.update(payload.encode("utf-8"))
    return digest.hexdigest()


def validity(subcategory: str) -> str:
    return "pass" if str(subcategory).casefold().startswith("dat/") else "fail"


def scene(subcategory: str) -> str:
    normalised = str(subcategory).casefold()
    if "bien hieu" in normalised:
        return "sign"
    if "trung bay" in normalised:
        return "display"
    return "fraud"


def find_visual_conflicts(
    embeddings: np.ndarray,
    records: Sequence[ReferenceRecord],
    threshold: float = 0.995,
) -> VisualConflictReport:
    """Find near-identical references carrying opposite validity labels."""

    vectors = np.asarray(embeddings, dtype=np.float32)
    if vectors.ndim != 2 or vectors.shape[0] != len(records):
        raise ValueError("Embeddings and reference records must have matching rows")
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("Visual-conflict threshold must be between 0 and 1")

    excluded: set[int] = set()
    pairs: list[tuple[int, int, float]] = []
    for start in range(0, len(records), 256):
        scores = vectors[start : start + 256] @ vectors.T
        for local_index, row in enumerate(scores):
            left = start + local_index
            for right_value in np.flatnonzero(row >= threshold):
                right = int(right_value)
                if right <= left:
                    continue
                same_scene = scene(records[left].effective_subcategory) == scene(
                    records[right].effective_subcategory
                )
                opposite_validity = validity(
                    records[left].effective_subcategory
                ) != validity(records[right].effective_subcategory)
                if same_scene and opposite_validity:
                    excluded.update((left, right))
                    pairs.append((left, right, float(row[right])))
    return VisualConflictReport(frozenset(excluded), tuple(pairs))
