"""Cloud migration compatibility for the one-off KPI sample run.

Historical image decisions are joined by the exact MobiWork image URL so the
sample does not redownload thousands of already-reviewed photos just to recover
SHA256 values. Only URLs absent from the legacy export are downloaded/scored.
The module also sanitizes impossible blank customer IDs while bootstrapping the
compact customer-history master.
"""
from __future__ import annotations

import csv
import hashlib
import logging
import os
from io import BytesIO, StringIO
from pathlib import PurePosixPath

import pandas as pd

from project_paths import SCORE_CACHE_DB
from scoring.legacy_seed import _payload
from scoring.records import assign_record_ids, build_audit_record, technical_failure_payload
from scoring.score_cache import ScoreCache


LOG = logging.getLogger("mobiwork_sync")


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

            pending_indices = [i for i in range(len(rows)) if i not in legacy_indices]
            pending_rows = [rows[i] for i in pending_indices]
            downloads = pipeline._download_image_rows(
                source, runtime_client, drive_id, pending_rows
            ) if pending_rows else []
            valid_pending = [
                local_index
                for local_index, (_remote, content, error) in enumerate(downloads)
                if content is not None and error is None
            ]
            stats = {
                "images": len(rows),
                "legacy_url_hits": len(legacy_indices),
                "stored_images_loaded": len(valid_pending),
                "missing_or_failed_images": len(pending_rows) - len(valid_pending),
                "remote_seeded_scores": cache.seed(
                    pipeline._load_remote_score_rows(runtime_client, drive_id, period_start),
                    signature,
                ),
                "cache_hits": 0,
                "new_unique_scores": 0,
            }

            if valid_pending:
                contents = [downloads[i][1] for i in valid_pending]
                before = sum(
                    cache.get(signature, hashlib.sha256(content).hexdigest()) is not None
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
            "Legacy URL reuse: %s/%s image rows reused; %s require stored-image lookup",
            len(legacy_indices),
            len(rows),
            len(pending_rows),
        )
        return pd.DataFrame(item for item in output if item is not None), stats, signature

    pipeline._build_image_results = build_image_results
    return len(legacy_by_url)
