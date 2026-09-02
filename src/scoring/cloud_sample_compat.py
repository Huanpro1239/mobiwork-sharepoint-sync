"""Cloud scoring compatibility, migration reuse, and bounded production catch-up."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
from io import StringIO
from pathlib import PurePosixPath

import pandas as pd

from project_paths import SCORE_CACHE_DB
from scoring.legacy_seed import _payload
from scoring.production_queue import (
    PendingURLGroup,
    advance_retry_attempts,
    legacy_requires_rescore,
    select_pending_url_groups,
)
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
        status = str(payload.get("Trạng Thái Quyết Định", "")).strip().upper()
        if status in {"PENDING_SCORE", "TECHNICAL_FAILURE"}:
            continue
        output[url] = {
            "image_sha256": image_sha,
            "payload": payload,
            "Tên File": str(row.get("Tên File", "")).strip(),
        }
    return output


def _remote_cache_seed_rows(
    scores_by_url: dict[str, dict[str, object]],
    pipeline_signature: str,
) -> list[dict[str, object]]:
    """Materialize only reusable records for ``ScoreCache.seed``."""

    return [
        {
            "hinh_anh": url,
            "pipeline_signature": pipeline_signature,
            "image_sha256": prior["image_sha256"],
            "score_payload_json": json.dumps(
                prior["payload"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        for url, prior in scores_by_url.items()
    ]


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


def _production_max_technical_retries() -> int:
    raw = os.environ.get("AI_PRODUCTION_MAX_TECHNICAL_RETRIES", "3").strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(
            "AI_PRODUCTION_MAX_TECHNICAL_RETRIES must be an integer"
        ) from error
    if value < 1:
        raise ValueError("AI_PRODUCTION_MAX_TECHNICAL_RETRIES must be >= 1")
    return value


def _load_technical_attempts(
    client,
    drive_id: str,
    period_start: pd.Timestamp,
    pipeline_signature: str,
) -> dict[str, int]:
    root = os.environ.get("KPI_SHAREPOINT_ROOT", "KPI").strip().strip("/") or "KPI"
    folder = f"{root}/{period_start:%Y-%m}"
    remotes = (
        f"{folder}/scoring_checkpoint_manifest.json",
        f"{folder}/run_manifest.json",
    )
    attempts: dict[str, int] = {}
    for remote in remotes:
        # download_json returns None for a real 404. Transport and malformed JSON
        # errors deliberately propagate so a transient read cannot reset retries.
        manifest = client.download_json(drive_id, remote)
        if manifest is None:
            continue
        if not isinstance(manifest, dict):
            raise TypeError(f"Retry-state manifest {remote!r} must be an object")
        if str(manifest.get("pipeline_signature", "")).strip() != pipeline_signature:
            continue
        scoring = manifest.get("scoring")
        if scoring is None:
            continue
        if not isinstance(scoring, dict):
            raise TypeError(f"Retry-state scoring section in {remote!r} must be an object")
        raw_attempts = scoring.get("technical_attempts_by_url")
        if raw_attempts is None:
            continue
        if not isinstance(raw_attempts, dict):
            raise TypeError(
                f"technical_attempts_by_url in {remote!r} must be an object"
            )
        for key, value in raw_attempts.items():
            normalized = str(key).strip()
            if not normalized:
                continue
            try:
                count = max(0, int(value))
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"Invalid retry count for {normalized!r} in {remote!r}: {value!r}"
                ) from error
            if count:
                attempts[normalized] = max(attempts.get(normalized, 0), count)
    return attempts


def _download_unique_url_groups(
    download_rows,
    source,
    client,
    drive_id: str,
    rows: list[dict[str, object]],
    groups: list[PendingURLGroup],
) -> list[tuple[str | None, bytes | None, Exception | None]]:
    """Try each occurrence path until one copy of a URL can be loaded."""

    results: list[tuple[str | None, bytes | None, Exception | None] | None] = [
        None
    ] * len(groups)
    max_occurrences = max((len(group.indices) for group in groups), default=0)
    for occurrence in range(max_occurrences):
        candidates = [
            (group_index, rows[group.indices[occurrence]])
            for group_index, group in enumerate(groups)
            if results[group_index] is None and occurrence < len(group.indices)
        ]
        if not candidates:
            continue
        downloaded = download_rows(
            source,
            client,
            drive_id,
            [row for _group_index, row in candidates],
        )
        if len(downloaded) != len(candidates):
            raise RuntimeError("Image downloader returned an unexpected result count")
        for (group_index, _row), result in zip(candidates, downloaded, strict=True):
            remote, content, error = result
            if content is not None and error is None:
                results[group_index] = result
            elif occurrence + 1 >= len(groups[group_index].indices):
                results[group_index] = (remote, content, error)

    return [
        result
        if result is not None
        else (None, None, FileNotFoundError("No stored occurrence is available"))
        for result in results
    ]


def _legacy_auto_payload(row: dict[str, str]) -> dict[str, object]:
    payload = _payload(row)
    payload["Trạng Thái Quyết Định"] = "LEGACY_AUTO_REUSED"
    basis = str(payload.get("Căn Cứ Nhận Diện", "")).strip()
    payload["Căn Cứ Nhận Diện"] = (
        f"{basis} [LEGACY_AUTO_REUSED]".strip()
    )
    return payload


def install_legacy_url_scoring(client) -> int:
    """Patch cloud scoring with legacy reuse and resumable V2.3 production batches."""

    import score_kpi_pipeline as pipeline
    from scoring.service import ImageScoringService

    legacy_by_url = _load_legacy_by_url(client)

    def build_image_results(source, runtime_client, drive_id, rows, period_start):
        record_ids = assign_record_ids(rows)
        output: list[dict[str, object] | None] = [None] * len(rows)
        legacy_auto_indices: set[int] = set()
        legacy_rescore_indices: set[int] = set()
        remote_indices: set[int] = set()

        cache = ScoreCache(SCORE_CACHE_DB)
        with ImageScoringService(cache=cache) as service:
            signature = service.pipeline_signature
            remote_rows = pipeline._load_remote_score_rows(
                runtime_client, drive_id, period_start
            )
            remote_by_url = _remote_scores_by_url(remote_rows, signature)
            remote_seeded = cache.seed(
                _remote_cache_seed_rows(remote_by_url, signature),
                signature,
            )

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
                if legacy_requires_rescore(legacy):
                    legacy_rescore_indices.add(index)
                    continue
                record = build_audit_record(
                    row,
                    record_ids[index],
                    signature,
                    "",
                    _legacy_auto_payload(legacy),
                )
                legacy_name = str(legacy.get("Tên File", "")).strip()
                if legacy_name:
                    record["Tên File"] = legacy_name
                output[index] = record
                legacy_auto_indices.add(index)

            candidate_indices = [
                index for index, record in enumerate(output) if record is None
            ]
            sample_limit = _sample_pending_limit()
            production_limit = _production_pending_limit() if not sample_limit else 0
            sample_selection = _sample_pending_selection()
            batch_mode = (
                "sample"
                if sample_limit
                else "production"
                if production_limit
                else "unbounded"
            )
            batch_limit = sample_limit or production_limit
            group_selection = sample_selection if sample_limit else "oldest"
            max_attempts = _production_max_technical_retries()
            attempts_by_url = (
                {}
                if sample_limit
                else _load_technical_attempts(
                    runtime_client,
                    drive_id,
                    period_start,
                    signature,
                )
            )
            selection = select_pending_url_groups(
                rows,
                candidate_indices=candidate_indices,
                attempts_by_url=attempts_by_url,
                limit=batch_limit,
                max_attempts=max_attempts,
                selection=group_selection,
            )
            all_groups = (*selection.selected, *selection.deferred, *selection.blocked)
            active_keys = {group.key for group in all_groups}
            attempts_by_url = {
                key: value
                for key, value in attempts_by_url.items()
                if key in active_keys
            }

            for group in selection.deferred:
                reason = (
                    "unmatched image omitted from bounded cloud validation"
                    if sample_limit
                    else "production backlog deferred to the next checkpoint batch"
                )
                for original_index in group.indices:
                    output[original_index] = build_audit_record(
                        rows[original_index],
                        record_ids[original_index],
                        signature,
                        "",
                        _pending_payload(reason),
                    )

            for group in selection.blocked:
                payload = technical_failure_payload(
                    "RETRY_EXHAUSTED: ảnh không khả dụng sau "
                    f"{group.attempts} lần thử"
                )
                for original_index in group.indices:
                    output[original_index] = build_audit_record(
                        rows[original_index],
                        record_ids[original_index],
                        signature,
                        "",
                        payload,
                    )

            selected_groups = list(selection.selected)
            downloads = (
                _download_unique_url_groups(
                    pipeline._download_image_rows,
                    source,
                    runtime_client,
                    drive_id,
                    rows,
                    selected_groups,
                )
                if selected_groups
                else []
            )
            valid_selected = [
                local_index
                for local_index, (_remote, content, error) in enumerate(downloads)
                if content is not None and error is None
            ]
            succeeded_keys: set[str] = set()
            failed_keys: set[str] = set()
            download_failed_keys: set[str] = set()
            cache_hits = 0
            new_unique_scores = 0
            preexisting_matches = 0

            if valid_selected:
                contents = [downloads[index][1] for index in valid_selected]
                preexisting_matches = sum(
                    cache.get(signature, hashlib.sha256(content).hexdigest())
                    is not None
                    for content in contents
                    if content is not None
                )
                scored = service.score_contents(
                    content for content in contents if content is not None
                )
                cache_hits = sum(int(item.cache_hit) for item in scored)
                new_unique_scores = len(
                    {item.image_sha256 for item in scored if not item.cache_hit}
                )
                for local_index, outcome in zip(
                    valid_selected,
                    scored,
                    strict=True,
                ):
                    group = selected_groups[local_index]
                    remote = downloads[local_index][0]
                    status = str(
                        outcome.payload.get("Trạng Thái Quyết Định", "")
                    ).strip().upper()
                    if status == "TECHNICAL_FAILURE":
                        failed_keys.add(group.key)
                    else:
                        succeeded_keys.add(group.key)
                    for original_index in group.indices:
                        record = build_audit_record(
                            rows[original_index],
                            record_ids[original_index],
                            signature,
                            outcome.image_sha256,
                            outcome.payload,
                        )
                        if remote:
                            record["Tên File"] = PurePosixPath(remote).name
                        output[original_index] = record

            for local_index, (_remote, _content, error) in enumerate(downloads):
                group = selected_groups[local_index]
                if local_index in valid_selected:
                    continue
                failed_keys.add(group.key)
                download_failed_keys.add(group.key)
                payload = technical_failure_payload(
                    f"IMAGE_SOURCE_ERROR {type(error).__name__}: {error}"
                    if error
                    else "Ảnh SharePoint không khả dụng"
                )
                for original_index in group.indices:
                    output[original_index] = build_audit_record(
                        rows[original_index],
                        record_ids[original_index],
                        signature,
                        "",
                        payload,
                    )

            retry_state = advance_retry_attempts(
                attempts_by_url=attempts_by_url,
                succeeded_urls=succeeded_keys,
                failed_urls=failed_keys,
                max_attempts=max_attempts,
            )
            newly_blocked = failed_keys & retry_state.blocked_urls
            for group in selected_groups:
                if group.key not in newly_blocked:
                    continue
                for original_index in group.indices:
                    record = output[original_index]
                    if record is not None:
                        basis = str(record.get("Căn Cứ Nhận Diện", "")).strip()
                        record["Căn Cứ Nhận Diện"] = (
                            f"{basis} [RETRY_EXHAUSTED]".strip()
                        )

            deferred_keys = {group.key for group in selection.deferred}
            retryable_failed = failed_keys & retry_state.retryable_urls
            production_pending_unique = (
                len(deferred_keys | retryable_failed) if not sample_limit else 0
            )
            selected_row_count = sum(
                len(group.indices) for group in selected_groups
            )
            deferred_row_count = sum(
                len(group.indices) for group in selection.deferred
            )
            retryable_failed_rows = sum(
                len(group.indices)
                for group in selected_groups
                if group.key in retryable_failed
            )
            production_pending_rows = (
                deferred_row_count + retryable_failed_rows
                if not sample_limit
                else 0
            )
            technical_keys = {
                *(group.key for group in selection.blocked),
                *failed_keys,
            }
            technical_rows = sum(
                len(group.indices)
                for group in all_groups
                if group.key in technical_keys
            )
            loaded_rows = sum(
                len(selected_groups[index].indices)
                for index in valid_selected
            )
            download_failed_rows = sum(
                len(group.indices)
                for group in selected_groups
                if group.key in download_failed_keys
            )
            unique_input_keys = {
                str(row.get("hinh_anh", "")).strip() or f"record:{record_ids[index]}"
                for index, row in enumerate(rows)
            }
            remote_urls = {
                str(rows[index].get("hinh_anh", "")).strip()
                for index in remote_indices
            }
            legacy_auto_urls = {
                str(rows[index].get("hinh_anh", "")).strip()
                for index in legacy_auto_indices
            }
            legacy_rescore_urls = {
                str(rows[index].get("hinh_anh", "")).strip()
                for index in legacy_rescore_indices
            }
            stats = {
                "images": len(rows),
                "unique_images": len(unique_input_keys),
                "legacy_url_hits": (
                    len(legacy_auto_indices) + len(legacy_rescore_indices)
                ),
                "legacy_url_hits_unique": len(
                    legacy_auto_urls | legacy_rescore_urls
                ),
                "legacy_auto_reused": len(legacy_auto_indices),
                "legacy_auto_reused_unique": len(legacy_auto_urls),
                "legacy_rescore_candidates": len(legacy_rescore_indices),
                "legacy_rescore_candidates_unique": len(legacy_rescore_urls),
                "remote_url_hits": len(remote_indices),
                "current_model_reused": len(remote_indices),
                "current_model_reused_unique": len(remote_urls),
                "pending_before_batch_limit": sum(
                    len(group.indices) for group in all_groups
                ),
                "pending_before_batch_limit_unique": len(all_groups),
                "sample_pending_limit": sample_limit,
                "sample_pending_selection": sample_selection,
                "sample_scored_pending_images": (
                    selected_row_count if sample_limit else 0
                ),
                "sample_scored_pending_unique": (
                    len(selected_groups) if sample_limit else 0
                ),
                "sample_skipped_images": (
                    deferred_row_count if sample_limit else 0
                ),
                "sample_skipped_unique": (
                    len(selection.deferred) if sample_limit else 0
                ),
                "production_batch_limit": production_limit,
                "production_batch_scored_images": (
                    selected_row_count if not sample_limit else 0
                ),
                "production_batch_scored_unique": (
                    len(selected_groups) if not sample_limit else 0
                ),
                "production_pending_remaining": production_pending_rows,
                "production_pending_remaining_unique": production_pending_unique,
                "stored_images_loaded": loaded_rows,
                "stored_images_loaded_unique": len(valid_selected),
                "missing_or_failed_images": download_failed_rows,
                "missing_or_failed_images_unique": len(download_failed_keys),
                "technical_failure_count": technical_rows,
                "technical_failure_unique": len(technical_keys),
                "retryable_technical_unique": len(retryable_failed),
                "blocked_technical_unique": len(retry_state.blocked_urls),
                "technical_attempts_by_url": retry_state.attempts_by_url,
                "technical_retry_limit": max_attempts,
                "remote_seeded_scores": remote_seeded,
                "cache_hits": cache_hits,
                "new_unique_scores": new_unique_scores,
                "preexisting_local_cache_matches": int(preexisting_matches),
            }

        if any(item is None for item in output):
            raise RuntimeError("Cloud URL scoring left unresolved result rows")
        LOG.info(
            "Cloud URL reuse: legacy_auto=%s legacy_rescore=%s remote_current=%s "
            "total_rows=%s unique_pending=%s mode=%s attempted_unique=%s "
            "deferred_unique=%s blocked_unique=%s",
            len(legacy_auto_indices),
            len(legacy_rescore_indices),
            len(remote_indices),
            len(rows),
            len(all_groups),
            batch_mode,
            len(selected_groups),
            len(selection.deferred),
            len(retry_state.blocked_urls),
        )
        return (
            pd.DataFrame(item for item in output if item is not None),
            stats,
            signature,
        )

    pipeline._build_image_results = build_image_results
    return len(legacy_by_url)
