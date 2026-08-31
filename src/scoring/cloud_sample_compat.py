"""Cloud migration compatibility for the one-off KPI sample run.

Historical image decisions are joined by the exact MobiWork image URL so the
sample does not redownload thousands of already-reviewed photos just to recover
SHA256 values. Only URLs absent from the legacy export are downloaded/scored.
An optional sample-only cap can bound how many unmatched images are inferred
while still carrying all rows through the workbook as auditable technical
sample skips. The module also sanitizes impossible blank customer IDs while
bootstrapping the compact customer-history master.
"""
from __future__ import annotations

import csv
import hashlib
import logging
import os
from io import StringIO
from pathlib import PurePosixPath

import pandas as pd

from project_paths import SCORE_CACHE_DB
from scoring.legacy_seed import _payload
from scoring.records import assign_record_ids, build_audit_record, technical_failure_payload
from scoring.score_cache import ScoreCache


LOG = logging.getLogger("mobiwork_sync")


def _sample_pending_limit() -> int:
    """Return the sample-only unmatched-image inference cap; zero means unlimited."""

    raw = os.environ.get("AI_SAMPLE_MAX_PENDING_IMAGES", "").strip()
    if not raw:
        return 0
    try:
        limit = int(raw)
    except ValueError as error:
        raise ValueError("AI_SAMPLE_MAX_PENDING_IMAGES must be an integer") from error
    if limit < 0:
        raise ValueError("AI_SAMPLE_MAX_PENDING_IMAGES must be >= 0")
    return limit


def install_history_sanitizer() -> None:
    """Drop history rows that cannot ever join to a customer KPI record."""

    import kpi.customer_history as history_module

    original = history_module._prepare_existing
    if getattr(original, "_cloud_blank_id_sanitizer", False):
        return

    def sanitized(frame: pd.DataFrame) -> pd.DataFrame:
        if not frame.empty and "ma_kh" in frame.columns:
            cleaned = frame.copy()
            normalized = cleaned["ma_kh"].map(history_module._clean_text)
            valid = normalized.ne("") & normalized.str.casefold().ne("nan")
            dropped = int((~valid).sum())
            if dropped:
                LOG.warning(
                    "Customer history bootstrap ignored %s row(s) with blank/invalid ma_kh",
                    dropped,
                )
            cleaned = cleaned.loc[valid].copy()
            cleaned["ma_kh"] = normalized.loc[valid]
            frame = cleaned
        return original(frame)

    sanitized._cloud_blank_id_sanitizer = True  # type: ignore[attr-defined]
    history_module._prepare_existing = sanitized


def _load_legacy_by_url(client) -> dict[str, dict[str, str]]:
    remote = os.environ.get("AI_LEGACY_SCORE_REMOTE", "").strip().strip("/")
    asset_drive = os.environ.get("AI_ASSET_DRIVE_ID", "").strip()
    if not remote or not asset_drive:
        return {}
    raw = client.download_file_bytes(asset_drive, remote)
    if not raw:
        raise FileNotFoundError(f"Legacy score CSV missing: {remote}")
    rows = csv.DictReader(StringIO(raw.decode("utf-8-sig")))
    result: dict[str, dict[str, str]] = {}
    for source in rows:
        row = dict(source)
        url = str(row.get("hinh_anh", "")).strip()
        label = str(row.get("Phân Loại AI", "")).strip()
        if url and label:
            result[url] = row
    return result


def install_legacy_url_scoring(client) -> int:
    """Patch the cloud run so reviewed historical URLs bypass model inference."""

    import score_kpi_pipeline as pipeline
    from scoring.service import ImageScoringService

    legacy_by_url = _load_legacy_by_url(client)
    if not legacy_by_url:
        return 0

    def build_image_results(source, runtime_client, drive_id, rows, period_start):
        record_ids = assign_record_ids(rows)
        output: list[dict[str, object] | None] = [None] * len(rows)
        legacy_indices: set[int] = set()

        cache = ScoreCache(SCORE_CACHE_DB)
        with ImageScoringService(cache=cache) as service:
            signature = service.pipeline_signature
            for index, row in enumerate(rows):
                url = str(row.get("hinh_anh", "")).strip()
                legacy = legacy_by_url.get(url)
                if legacy is None:
                    continue
                record = build_audit_record(
                    row,
                    record_ids[index],
                    signature,
                    "",
                    _payload(legacy),
                )
                legacy_name = str(legacy.get("Tên File", "")).strip()
                if legacy_name:
                    record["Tên File"] = legacy_name
                output[index] = record
                legacy_indices.add(index)

            all_pending_indices = [
                i for i in range(len(rows)) if i not in legacy_indices
            ]
            pending_indices = list(all_pending_indices)
            skipped_indices: list[int] = []
            sample_limit = _sample_pending_limit()
            if sample_limit and len(pending_indices) > sample_limit:
                # Prefer the most recent rows because SharePoint monthly masters
                # are chronological and this probe exists to validate the newest
                # stored-image path plus current model runtime.
                skipped_indices = pending_indices[:-sample_limit]
                pending_indices = pending_indices[-sample_limit:]
                for original_index in skipped_indices:
                    payload = technical_failure_payload(
                        "SAMPLE_SKIPPED: unmatched image omitted from bounded cloud validation"
                    )
                    output[original_index] = build_audit_record(
                        rows[original_index],
                        record_ids[original_index],
                        signature,
                        "",
                        payload,
                    )
                LOG.warning(
                    "Bounded cloud sample: %s unmatched image rows total; scoring latest %s and marking %s as SAMPLE_SKIPPED",
                    len(all_pending_indices),
                    len(pending_indices),
                    len(skipped_indices),
                )

            pending_rows = [rows[i] for i in pending_indices]
            downloads = (
                pipeline._download_image_rows(
                    source, runtime_client, drive_id, pending_rows
                )
                if pending_rows
                else []
            )
            valid_pending = [
                local_index
                for local_index, (_remote, content, error) in enumerate(downloads)
                if content is not None and error is None
            ]
            stats = {
                "images": len(rows),
                "legacy_url_hits": len(legacy_indices),
                "pending_before_sample_limit": len(all_pending_indices),
                "sample_pending_limit": sample_limit,
                "sample_scored_pending_images": len(pending_indices),
                "sample_skipped_images": len(skipped_indices),
                "stored_images_loaded": len(valid_pending),
                "missing_or_failed_images": len(pending_rows) - len(valid_pending),
                "remote_seeded_scores": cache.seed(
                    pipeline._load_remote_score_rows(
                        runtime_client, drive_id, period_start
                    ),
                    signature,
                ),
                "cache_hits": 0,
                "new_unique_scores": 0,
            }

            if valid_pending:
                contents = [downloads[i][1] for i in valid_pending]
                before = sum(
                    cache.get(signature, hashlib.sha256(content).hexdigest())
                    is not None
                    for content in contents
                    if content is not None
                )
                scored = service.score_contents(
                    content for content in contents if content is not None
                )
                stats["cache_hits"] = sum(int(item.cache_hit) for item in scored)
                stats["new_unique_scores"] = len(
                    {item.image_sha256 for item in scored if not item.cache_hit}
                )
                stats["preexisting_local_cache_matches"] = int(before)
                for local_index, outcome in zip(valid_pending, scored, strict=True):
                    original_index = pending_indices[local_index]
                    remote = downloads[local_index][0]
                    row = rows[original_index]
                    record = build_audit_record(
                        row,
                        record_ids[original_index],
                        signature,
                        outcome.image_sha256,
                        outcome.payload,
                    )
                    if remote:
                        record["Tên File"] = PurePosixPath(remote).name
                    output[original_index] = record

            for local_index, (_remote, _content, error) in enumerate(downloads):
                original_index = pending_indices[local_index]
                if output[original_index] is not None:
                    continue
                payload = technical_failure_payload(
                    f"IMAGE_SOURCE_ERROR {type(error).__name__}: {error}"
                    if error
                    else "Ảnh SharePoint không khả dụng"
                )
                output[original_index] = build_audit_record(
                    rows[original_index],
                    record_ids[original_index],
                    signature,
                    "",
                    payload,
                )

        if any(item is None for item in output):
            raise RuntimeError("Cloud legacy URL scoring left unresolved result rows")
        LOG.info(
            "Legacy URL reuse: %s/%s image rows reused; %s unmatched before sample limit; %s scored by stored-image lookup; %s sample-skipped",
            len(legacy_indices),
            len(rows),
            len(all_pending_indices),
            len(pending_indices),
            len(skipped_indices),
        )
        return (
            pd.DataFrame(item for item in output if item is not None),
            stats,
            signature,
        )

    pipeline._build_image_results = build_image_results
    return len(legacy_by_url)
