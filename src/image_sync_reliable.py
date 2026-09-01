from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from image_sync import (
    ImageMetadataSource,
    ImageStorage,
    ImageSyncConfig,
    _IMAGE_EXTENSIONS,
    _cleanup_old_months,
    _download_image,
    _plan_candidates,
    _remote_image_path,
    _resolve_start_date,
    _state_path,
    retained_months,
)
from mobiwork import ReportConfig

LOG = logging.getLogger("mobiwork_sync")


def _batch_limit() -> int:
    raw = os.environ.get("IMAGE_SYNC_MAX_UPLOADS_PER_RUN", "1500").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("IMAGE_SYNC_MAX_UPLOADS_PER_RUN must be an integer") from exc
    if not 1 <= value <= 10000:
        raise ValueError("IMAGE_SYNC_MAX_UPLOADS_PER_RUN must be between 1 and 10000")
    return value


def _runtime_limit_seconds() -> int:
    """Return the soft wall-clock budget used before the runner hard timeout.

    The default is intentionally well below GitHub's job timeout. A single slow
    HTTP request can still finish after the budget is reached, so the gap to the
    runner timeout must be large enough for request retries and final checkpoint
    writes.
    """

    raw = os.environ.get("IMAGE_SYNC_MAX_RUNTIME_SECONDS", "7200").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("IMAGE_SYNC_MAX_RUNTIME_SECONDS must be an integer") from exc
    if not 60 <= value <= 21600:
        raise ValueError("IMAGE_SYNC_MAX_RUNTIME_SECONDS must be between 60 and 21600")
    return value


def _url_digest(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]


def _provisional_extension(url: str) -> str:
    suffix = PurePosixPath(unquote(urlsplit(url).path)).suffix.lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    return suffix if suffix in _IMAGE_EXTENSIONS else ".jpg"


def _is_nonempty_file(item: dict[str, Any]) -> bool:
    if "folder" in item:
        return False
    try:
        return int(item.get("size") or 0) > 0
    except (TypeError, ValueError):
        return False


class RemoteFolderIndex:
    """Cache folder children and recognize stored images by URL digest."""

    def __init__(self, storage: ImageStorage, drive_id: str) -> None:
        self.storage = storage
        self.drive_id = drive_id
        self._children: dict[str, dict[str, int]] = {}
        self._failed_folders: set[str] = set()
        self.list_calls = 0
        self.list_failures = 0

    def _load(self, folder: str) -> dict[str, int] | None:
        if folder in self._children:
            return self._children[folder]
        if folder in self._failed_folders:
            return None
        try:
            children = self.storage.list_folder_children(self.drive_id, folder)
            self.list_calls += 1
            indexed: dict[str, int] = {}
            for item in children:
                if not _is_nonempty_file(item):
                    continue
                name = str(item.get("name") or "").strip()
                if name:
                    indexed[name] = int(item.get("size") or 0)
            self._children[folder] = indexed
            return indexed
        except Exception:
            self.list_failures += 1
            self._failed_folders.add(folder)
            LOG.exception("Unable to list SharePoint image folder: %s", folder)
            return None

    def contains_digest(self, folder: str, digest: str) -> bool | None:
        indexed = self._load(folder)
        if indexed is None:
            return None
        marker = f"_{digest}."
        return any(marker in name.casefold() for name in indexed)

    def remember(self, remote_path: str, size: int) -> None:
        folder, _, name = remote_path.rpartition("/")
        if folder in self._children and name:
            self._children[folder][name] = size


def _deduplicate_candidates(planned: list[Any], cfg: ImageSyncConfig) -> list[Any]:
    unique: dict[tuple[str, str], Any] = {}
    for candidate in planned:
        folder, _ = _remote_image_path(
            cfg,
            candidate.record,
            candidate.url,
            candidate.image_date,
            candidate.image_index,
            _provisional_extension(candidate.url),
        )
        key = (folder, _url_digest(candidate.url))
        current = unique.get(key)
        order = (candidate.image_date, candidate.image_index, candidate.url)
        if current is None or order < (
            current.image_date,
            current.image_index,
            current.url,
        ):
            unique[key] = candidate
    return sorted(
        unique.values(),
        key=lambda item: (
            item.image_date,
            str(item.record.get(cfg.employee_field) or ""),
            str(item.record.get(cfg.customer_field) or ""),
            item.image_index,
            item.url,
        ),
    )


def _fallback_exact_present(
    storage: ImageStorage,
    drive_id: str,
    cfg: ImageSyncConfig,
    candidate: Any,
) -> bool:
    _, path = _remote_image_path(
        cfg,
        candidate.record,
        candidate.url,
        candidate.image_date,
        candidate.image_index,
        _provisional_extension(candidate.url),
    )
    item = storage.get_item_by_path(drive_id, path)
    return bool(item and _is_nonempty_file(item))


def run_image_sync_reliable(
    reports: list[ReportConfig],
    source: ImageMetadataSource,
    storage: ImageStorage | None,
    drive_id: str | None,
    dry_run: bool,
    today: date,
    cfg: ImageSyncConfig | None = None,
) -> dict[str, Any]:
    """Reconcile expected image targets in bounded, resumable batches."""

    started = time.monotonic()
    cfg = cfg or ImageSyncConfig.from_env()
    limit = _batch_limit()
    runtime_limit = _runtime_limit_seconds()
    deadline = started + runtime_limit
    result: dict[str, Any] = {
        "enabled": cfg.enabled,
        "status": "disabled" if not cfg.enabled else "running",
        "root_folder": cfg.root_folder,
        "retained_months": sorted(retained_months(today)),
        "candidate_count": 0,
        "unique_target_count": 0,
        "duplicate_candidate_count": 0,
        "records_scanned": 0,
        "skipped_existing_count": 0,
        "attempted_missing_count": 0,
        "uploaded_count": 0,
        "failed_count": 0,
        "deferred_count": 0,
        "pending_remaining": 0,
        "downloaded_bytes": 0,
        "remote_folder_list_calls": 0,
        "remote_folder_list_failures": 0,
        "batch_limit": limit,
        "runtime_limit_seconds": runtime_limit,
        "runtime_budget_exhausted": False,
        "stop_reason": None,
        "deleted_month_folders": [],
    }
    if not cfg.enabled:
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        return result

    report = next((item for item in reports if item.key == cfg.source_report_key), None)
    if report is None:
        result["status"] = "failed"
        result["error"] = f"Image source report {cfg.source_report_key!r} is not enabled"
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        return result

    state: dict[str, Any] | None = None
    if storage and drive_id:
        state = storage.download_json(drive_id, _state_path(cfg))
    elif not dry_run:
        result["status"] = "failed"
        result["error"] = "SharePoint storage is unavailable for image sync"
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        return result

    from_date = _resolve_start_date(today, state, cfg.force_from_date)
    result["from_date"] = from_date.isoformat()
    result["to_date"] = today.isoformat()
    result["forced_from_date"] = cfg.force_from_date.isoformat() if cfg.force_from_date else None

    records = source.fetch_report_range(report, from_date, today)
    result["records_scanned"] = len(records)
    planned = _plan_candidates(records, cfg, from_date, today)
    unique = _deduplicate_candidates(planned, cfg)
    result["candidate_count"] = len(planned)
    result["unique_target_count"] = len(unique)
    result["duplicate_candidate_count"] = len(planned) - len(unique)

    if dry_run:
        result["status"] = "dry_run"
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        return result
    if storage is None or drive_id is None:
        raise RuntimeError("SharePoint storage unexpectedly unavailable")

    remote_index = RemoteFolderIndex(storage, drive_id)
    failures: list[dict[str, str]] = []
    deferred_dates: list[date] = []

    for index, candidate in enumerate(unique):
        # Stop before starting more remote work when the soft wall-clock budget is
        # exhausted. The remaining candidates are checkpointed and retried by the
        # next run, leaving a wide safety margin for state/manifest writes before a
        # GitHub runner can enforce its hard job timeout.
        if time.monotonic() >= deadline:
            remaining = unique[index:]
            result["deferred_count"] += len(remaining)
            deferred_dates.extend(item.image_date for item in remaining)
            result["runtime_budget_exhausted"] = True
            result["stop_reason"] = "runtime_budget"
            LOG.warning(
                "Image sync soft runtime budget exhausted after %.1fs; deferring %s targets",
                time.monotonic() - started,
                len(remaining),
            )
            break

        folder, provisional_path = _remote_image_path(
            cfg,
            candidate.record,
            candidate.url,
            candidate.image_date,
            candidate.image_index,
            _provisional_extension(candidate.url),
        )
        digest = _url_digest(candidate.url)
        try:
            present = remote_index.contains_digest(folder, digest)
            if present is True:
                result["skipped_existing_count"] += 1
                continue
            if present is None and _fallback_exact_present(storage, drive_id, cfg, candidate):
                result["skipped_existing_count"] += 1
                continue

            # Bound attempts, not only successful uploads. Persistent bad URLs therefore
            # cannot turn one repair into an unbounded multi-hour run.
            if result["attempted_missing_count"] >= limit:
                result["deferred_count"] += 1
                deferred_dates.append(candidate.image_date)
                if result["stop_reason"] is None:
                    result["stop_reason"] = "batch_limit"
                continue
            result["attempted_missing_count"] += 1

            content, content_type, extension = _download_image(source, candidate.url, cfg)
            folder, remote_path = _remote_image_path(
                cfg,
                candidate.record,
                candidate.url,
                candidate.image_date,
                candidate.image_index,
                extension,
            )
            if remote_path != provisional_path:
                present_after_download = remote_index.contains_digest(folder, digest)
                if present_after_download is True:
                    result["skipped_existing_count"] += 1
                    continue

            storage.upload_bytes(drive_id, remote_path, content, content_type)
            remote_index.remember(remote_path, len(content))
            result["uploaded_count"] += 1
            result["downloaded_bytes"] += len(content)
        except Exception as exc:
            result["failed_count"] += 1
            failures.append(
                {
                    "url": candidate.url[:500],
                    "date": candidate.image_date.isoformat(),
                    "employee": str(candidate.record.get(cfg.employee_field) or "")[:160],
                    "customer": str(candidate.record.get(cfg.customer_field) or "")[:160],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            LOG.exception("Unable to sync MobiWork image: %s", candidate.url)

    result["remote_folder_list_calls"] = remote_index.list_calls
    result["remote_folder_list_failures"] = remote_index.list_failures
    result["pending_remaining"] = result["failed_count"] + result["deferred_count"]
    completed = result["pending_remaining"] == 0
    resolved = result["skipped_existing_count"] + result["uploaded_count"]
    result["completeness_pct"] = (
        100.0
        if result["unique_target_count"] == 0
        else round(100.0 * resolved / result["unique_target_count"], 4)
    )

    pending_dates = [item["date"] for item in failures]
    pending_dates.extend(item.isoformat() for item in deferred_dates)
    retry_from_date = min(pending_dates, default=None)
    result["retry_from_date"] = retry_from_date
    result["deleted_month_folders"] = _cleanup_old_months(storage, drive_id, cfg, today)

    previous_completed = state.get("last_completed_sync_date") if state else None
    previous_successful = state.get("last_successful_sync_date") if state else None
    if failures:
        run_status = "partial_failure"
    elif result["deferred_count"]:
        run_status = "warming_up"
    else:
        run_status = "success"

    storage.upload_json(
        drive_id,
        _state_path(cfg),
        {
            "schema_version": 4,
            "last_completed_sync_date": today.isoformat() if completed else previous_completed,
            "last_successful_sync_date": today.isoformat() if completed else previous_successful,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "root_folder": cfg.root_folder,
            "source_report": cfg.source_report_key,
            "source_mode": "sharepoint_monthly_master",
            "retained_months": sorted(retained_months(today)),
            "last_run_status": run_status,
            "candidate_count": result["candidate_count"],
            "unique_target_count": result["unique_target_count"],
            "skipped_existing_count": result["skipped_existing_count"],
            "attempted_missing_count": result["attempted_missing_count"],
            "uploaded_count": result["uploaded_count"],
            "failed_count": result["failed_count"],
            "deferred_count": result["deferred_count"],
            "pending_remaining": result["pending_remaining"],
            "completeness_pct": result["completeness_pct"],
            "retry_from_date": retry_from_date,
            "runtime_limit_seconds": runtime_limit,
            "runtime_budget_exhausted": result["runtime_budget_exhausted"],
            "stop_reason": result["stop_reason"],
        },
    )

    result["status"] = run_status
    if failures:
        result["failures"] = failures[:50]
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result
