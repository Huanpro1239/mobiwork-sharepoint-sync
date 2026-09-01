"""Cloud scoring compatibility, migration reuse, and bounded production catch-up."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import time
from io import StringIO
from pathlib import PurePosixPath

import pandas as pd

from project_paths import SCORE_CACHE_DB
from scoring.legacy_seed import _payload
from scoring.records import assign_record_ids, build_audit_record, technical_failure_payload
from scoring.score_cache import ScoreCache


LOG = logging.getLogger("mobiwork_sync")


def _bounded_limit(name: str) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return 0
    try:
        limit = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if limit < 0:
        raise ValueError(f"{name} must be >= 0")
    return limit


def _sample_pending_limit() -> int:
    """Sample-only unmatched-image cap; zero means unlimited."""
    return _bounded_limit("AI_SAMPLE_MAX_PENDING_IMAGES")


def _production_pending_limit() -> int:
    """Production catch-up cap; zero means unlimited."""
    return _bounded_limit("AI_PRODUCTION_MAX_PENDING_IMAGES")


def _production_runtime_limit_seconds() -> int:
    """Soft wall-clock budget for production scoring; zero disables it."""
    value = _bounded_limit("AI_PRODUCTION_MAX_RUNTIME_SECONDS")
    if value and value < 300:
        raise ValueError("AI_PRODUCTION_MAX_RUNTIME_SECONDS must be 0 or >= 300")
    return value


def _score_chunk_size() -> int:
    raw = os.environ.get("AI_SCORE_CHUNK_SIZE", "50").strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("AI_SCORE_CHUNK_SIZE must be an integer") from error
    if not 1 <= value <= 500:
        raise ValueError("AI_SCORE_CHUNK_SIZE must be between 1 and 500")
    return value


def _sample_pending_selection() -> str:
    value = os.environ.get("AI_SAMPLE_PENDING_SELECTION", "latest").strip().casefold()
    if value not in {"latest", "oldest"}:
        raise ValueError("AI_SAMPLE_PENDING_SELECTION must be 'latest' or 'oldest'")
    return value


def _remote_scores_by_url(
    rows: list[dict[str, object]], pipeline_signature: str
) -> dict[str, dict[str, object]]:
    """Return reusable current-model records keyed by exact source URL."""
    output: dict[str, dict[str, object]] = {}
    for row in rows:
        url = str(row.get("hinh_anh", "")).strip()
        signature = str(row.get("pipeline_signature", "")).strip()
        image_sha = str(row.get("image_sha256", "")).strip()
        payload_text = row.get("score_payload_json")
        if not url or signature != pipeline_signature or not image_sha or not payload_text:
            continue
        try:
            payload = json.loads(str(payload_text))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        output[url] = {
            "image_sha256": image_sha,
            "payload": payload,
            "Tên File": str(row.get("Tên File", "")).strip(),
        }
    return output


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


def _pending_payload(reason: str) -> dict[str, object]:
    payload = technical_failure_payload(reason)
    payload["Trạng Thái Quyết Định"] = "PENDING_SCORE"
    payload["Căn Cứ Nhận Diện"] = f"Chờ chấm AI theo batch: {reason} [PENDING_SCORE]"
    return payload


def install_legacy_url_scoring(client) -> int:
    """Patch cloud scoring with legacy reuse and resumable V2.3 production batches."""
    import score_kpi_pipeline as pipeline
    from scoring.service import ImageScoringService

    legacy_by_url = _load_legacy_by_url(client)

    def build_image_results(source, runtime_client, drive_id, rows, period_start):
        started = time.monotonic()
        record_ids = assign_record_ids(rows)
        output: list[dict[str, object] | None] = [None] * len(rows)
        legacy_indices: set[int] = set()
        remote_indices: set[int] = set()

        cache = ScoreCache(SCORE_CACHE_DB)
        with ImageScoringService(cache=cache) as service:
            signature = service.pipeline_signature
            remote_rows = pipeline._load_remote_score_rows(runtime_client, drive_id, period_start)
            remote_seeded = cache.seed(remote_rows, signature)
            remote_by_url = _remote_scores_by_url(remote_rows, signature)

            for index, row in enumerate(rows):
                url = str(row.get("hinh_anh", "")).strip()
                prior = remote_by_url.get(url)
                if prior is not None:
                    record = build_audit_record(
                        row,
                        record_ids[index],
                        signature,
                        str(prior["image_sha256"]),
                        prior["payload"],
                    )
                    prior_name = str(prior.get("Tên File", "")).strip()
                    if prior_name:
                        record["Tên File"] = prior_name
                    output[index] = record
                    remote_indices.add(index)
                    continue

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

            reused_indices = legacy_indices | remote_indices
            all_pending_indices = [i for i in range(len(rows)) if i not in reused_indices]
            pending_indices = list(all_pending_indices)
            skipped_indices: list[int] = []

            sample_limit = _sample_pending_limit()
            production_limit = _production_pending_limit() if not sample_limit else 0
            runtime_limit = _production_runtime_limit_seconds() if production_limit else 0
            deadline = started + runtime_limit if runtime_limit else None
            chunk_size = _score_chunk_size()
            sample_selection = _sample_pending_selection()
            batch_mode = "sample" if sample_limit else "production" if production_limit else "unbounded"
            batch_limit = sample_limit or production_limit
            selection = sample_selection if sample_limit else "oldest"

            if batch_limit and len(pending_indices) > batch_limit:
                if selection == "oldest":
                    pending_indices = pending_indices[:batch_limit]
                else:
                    pending_indices = pending_indices[-batch_limit:]
                selected = set(pending_indices)
                skipped_indices = [i for i in all_pending_indices if i not in selected]
                for original_index in skipped_indices:
                    reason = (
                        "unmatched image omitted from bounded cloud validation"
                        if sample_limit
                        else "production backlog deferred to the next checkpoint batch"
                    )
                    output[original_index] = build_audit_record(
                        rows[original_index],
                        record_ids[original_index],
                        signature,
                        "",
                        _pending_payload(reason),
                    )
                LOG.warning(
                    "Bounded cloud scoring: %s unmatched rows; mode=%s selection=%s; selected=%s deferred=%s",
                    len(all_pending_indices),
                    batch_mode,
                    selection,
                    len(pending_indices),
                    len(skipped_indices),
                )

            pending_rows = [rows[i] for i in pending_indices]
            downloads = (
                pipeline._download_image_rows(source, runtime_client, drive_id, pending_rows)
                if pending_rows
                else []
            )
            valid_pending = [
                local_index
                for local_index, (_remote, content, error) in enumerate(downloads)
                if content is not None and error is None
            ]
            failed_downloads = len(pending_rows) - len(valid_pending)
            runtime_deferred = 0
            production_remaining = len(skipped_indices) + failed_downloads if production_limit else 0
            stats = {
                "images": len(rows),
                "legacy_url_hits": len(legacy_indices),
                "remote_url_hits": len(remote_indices),
                "pending_before_batch_limit": len(all_pending_indices),
                "sample_pending_limit": sample_limit,
                "sample_pending_selection": sample_selection,
                "sample_scored_pending_images": 0,
                "sample_skipped_images": len(skipped_indices) if sample_limit else 0,
                "production_batch_limit": production_limit,
                "production_selected_images": len(pending_indices) if production_limit else 0,
                "production_batch_scored_images": 0,
                "production_pending_remaining": production_remaining,
                "production_runtime_limit_seconds": runtime_limit,
                "runtime_budget_exhausted": False,
                "score_chunk_size": chunk_size,
                "stored_images_loaded": len(valid_pending),
                "missing_or_failed_images": failed_downloads,
                "remote_seeded_scores": remote_seeded,
                "cache_hits": 0,
                "new_unique_scores": 0,
                "preexisting_local_cache_matches": 0,
            }

            scored_count = 0
            if valid_pending:
                for start in range(0, len(valid_pending), chunk_size):
                    if deadline is not None and time.monotonic() >= deadline:
                        remaining_local = valid_pending[start:]
                        runtime_deferred = len(remaining_local)
                        for local_index in remaining_local:
                            original_index = pending_indices[local_index]
                            output[original_index] = build_audit_record(
                                rows[original_index],
                                record_ids[original_index],
                                signature,
                                "",
                                _pending_payload("production scoring runtime budget exhausted"),
                            )
                        stats["runtime_budget_exhausted"] = True
                        production_remaining += runtime_deferred
                        LOG.warning(
                            "Production scoring soft runtime budget exhausted after %.1fs; deferring %s downloaded images",
                            time.monotonic() - started,
                            runtime_deferred,
                        )
                        break

                    chunk_local = valid_pending[start : start + chunk_size]
                    contents = [downloads[i][1] for i in chunk_local]
                    before = sum(
                        cache.get(signature, hashlib.sha256(content).hexdigest()) is not None
                        for content in contents
                        if content is not None
                    )
                    scored = service.score_contents(
                        content for content in contents if content is not None
                    )
                    stats["cache_hits"] += sum(int(item.cache_hit) for item in scored)
                    stats["new_unique_scores"] += len(
                        {item.image_sha256 for item in scored if not item.cache_hit}
                    )
                    stats["preexisting_local_cache_matches"] += int(before)
                    for local_index, outcome in zip(chunk_local, scored, strict=True):
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
                    scored_count += len(scored)

            if sample_limit:
                stats["sample_scored_pending_images"] = scored_count
            if production_limit:
                stats["production_batch_scored_images"] = scored_count
                stats["production_pending_remaining"] = production_remaining
                stats["runtime_deferred_images"] = runtime_deferred

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
            raise RuntimeError("Cloud URL scoring left unresolved result rows")
        LOG.info(
            "Cloud URL reuse: legacy=%s remote_v23=%s total=%s unmatched=%s mode=%s selected=%s scored=%s deferred=%s duration=%.1fs",
            len(legacy_indices),
            len(remote_indices),
            len(rows),
            len(all_pending_indices),
            batch_mode,
            len(pending_indices),
            scored_count,
            (len(skipped_indices) + runtime_deferred),
            time.monotonic() - started,
        )
        return pd.DataFrame(item for item in output if item is not None), stats, signature

    pipeline._build_image_results = build_image_results
    return len(legacy_by_url)
