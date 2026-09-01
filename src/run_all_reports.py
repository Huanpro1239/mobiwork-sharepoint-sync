from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import main as core
from mobiwork import MobiWorkClient, ReportConfig
from monthly_master import (
    build_month_from_partitions,
    frames_from_records,
    is_legacy_report_file,
    master_filename,
    master_row_count,
    merge_partition,
    month_dates_through,
    read_master,
    write_master,
)
from sharepoint_semantic import SemanticSharePointClient


LOG = logging.getLogger("mobiwork_sync")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def incremental_target_dates(sync_scope: str, lookback_days: int) -> list[date]:
    """Resolve incremental dates in Vietnam local time."""
    scope = sync_scope.strip().casefold()
    today_vn = datetime.now(core.VN_TZ).date()

    if scope == "today":
        return [today_vn]
    if scope == "yesterday":
        return [today_vn - timedelta(days=1)]
    if scope == "lookback":
        return core.target_dates(lookback_days)
    raise ValueError("SYNC_SCOPE must be today, yesterday, or lookback")


def group_target_dates_by_month(target_dates: list[date]) -> list[list[date]]:
    """Group target dates by calendar month while keeping newest month first.

    Dates inside each month are returned oldest-to-newest so partition merges happen
    in natural business order. This lets one report/month use one SharePoint read and
    at most one publish even when a lookback spans many target dates.
    """
    groups: dict[tuple[int, int], list[date]] = {}
    for target_date in target_dates:
        key = (target_date.year, target_date.month)
        groups.setdefault(key, []).append(target_date)
    return [sorted(values) for values in groups.values()]


def build_clients(
    dry_run: bool,
) -> tuple[MobiWorkClient, SemanticSharePointClient | None, str | None]:
    mobiwork = MobiWorkClient.from_env()
    if dry_run:
        return mobiwork, None, None

    sharepoint = SemanticSharePointClient.from_env()
    drive_id = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
    if not drive_id:
        site_id = sharepoint.get_site_id()
        drive_id = sharepoint.get_drive_id(site_id)
    return mobiwork, sharepoint, drive_id


def _result_entry(cfg: ReportConfig, target_date: date) -> dict[str, Any]:
    return {
        "report": cfg.key,
        "report_name": cfg.name,
        "target_date": target_date.isoformat(),
        "month_master": f"{target_date:%Y-%m}",
        "status": "running",
    }


def _cleanup_legacy_files(
    sharepoint: SemanticSharePointClient,
    drive_id: str,
    remote_folder: str,
    report_name: str,
    canonical_name: str,
) -> list[str]:
    deleted: list[str] = []
    for item in sharepoint.list_folder_children(drive_id, remote_folder):
        if "folder" in item:
            continue
        name = str(item.get("name", "")).strip()
        if not name or not is_legacy_report_file(name, report_name, canonical_name):
            continue
        remote_path = f"{remote_folder}/{name}"
        if sharepoint.delete_path(drive_id, remote_path):
            deleted.append(name)
            LOG.info("Removed legacy SharePoint report file: %s", remote_path)
    return deleted


def _record_monthly_export(
    manifest: dict[str, Any],
    cfg: ReportConfig,
    path: Any,
    source_rows: int,
    master_rows: int,
    remote_folder: str,
    uploaded: dict[str, Any] | None,
    target_dates: list[date] | None = None,
) -> None:
    """Record one physical monthly-master preparation/publish operation."""
    core._record_export(
        manifest,
        cfg,
        path,
        source_rows,
        remote_folder,
        uploaded,
    )
    export = manifest["files"][-1]
    export["master_rows"] = master_rows
    if target_dates:
        export["target_dates"] = [value.isoformat() for value in target_dates]
        export["target_execution_count"] = len(target_dates)
    if uploaded:
        export["verification_mode"] = uploaded.get("verification_mode")
        export["semantic_match"] = uploaded.get("semantic_match")
        export["upload_skipped"] = bool(uploaded.get("upload_skipped", False))


def _build_or_update_month_group(
    cfg: ReportConfig,
    target_dates: list[date],
    mobiwork: MobiWorkClient,
    sharepoint: SemanticSharePointClient | None,
    drive_id: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Prepare one report/month with one SharePoint read and one final workbook write.

    When a canonical monthly master already exists, target-day fetches are isolated:
    one failed day is reported but does not block successful target dates in the same
    month. When the canonical master is missing, the month must be rebuilt from day 01
    through the latest requested date; any missing rebuild partition fails the whole
    group to avoid publishing an incomplete canonical workbook.
    """
    if not target_dates:
        raise ValueError("target_dates must not be empty")

    ordered_dates = sorted(set(target_dates))
    anchor = ordered_dates[-1]
    if any((value.year, value.month) != (anchor.year, anchor.month) for value in ordered_dates):
        raise ValueError("target_dates must belong to the same calendar month")

    remote_folder = f"{cfg.folder}/{anchor:%Y}/{anchor:%m}"
    canonical_name = master_filename(cfg.name, anchor)
    remote_path = f"{remote_folder}/{canonical_name}"
    target_set = set(ordered_dates)
    source_rows: dict[date, int] = {}
    errors: dict[date, str] = {}
    rebuilt = False
    rebuild_days = 0

    if dry_run:
        frames: dict[str, Any] | None = None
        for target_date in ordered_dates:
            try:
                records = mobiwork.fetch_report(cfg, target_date)
                incoming = frames_from_records(records, cfg.export_mode, target_date)
                if frames is None:
                    frames = build_month_from_partitions([], cfg.export_mode)
                frames = merge_partition(frames, incoming, target_date, cfg.export_mode)
                source_rows[target_date] = len(records)
            except Exception as exc:
                errors[target_date] = f"{type(exc).__name__}: {exc}"
                LOG.exception(
                    "Dry-run target failed while remaining dates continue: report=%s date=%s",
                    cfg.key,
                    target_date,
                )

        if frames is None or not source_rows:
            return {
                "path": None,
                "source_rows": source_rows,
                "errors": errors,
                "master_rows": 0,
                "month_rebuilt": False,
                "rebuild_days": 0,
                "remote_folder": remote_folder,
            }
        path = write_master(frames, cfg.name, anchor)
        return {
            "path": path,
            "source_rows": source_rows,
            "errors": errors,
            "master_rows": master_row_count(frames, cfg.export_mode),
            "month_rebuilt": False,
            "rebuild_days": 0,
            "remote_folder": remote_folder,
        }

    if not sharepoint or not drive_id:
        raise RuntimeError("SharePoint client is unavailable in production mode")

    existing_content = sharepoint.download_file_bytes(drive_id, remote_path)
    if existing_content is None:
        rebuilt = True
        rebuild_dates = month_dates_through(anchor)
        rebuild_days = len(rebuild_dates)
        partitions: list[tuple[date, list[dict[str, Any]]]] = []
        LOG.info(
            "Monthly master missing; rebuilding once report=%s month=%s days=%s target_dates=%s",
            cfg.key,
            anchor.strftime("%Y-%m"),
            rebuild_days,
            ",".join(value.isoformat() for value in ordered_dates),
        )
        for rebuild_date in rebuild_dates:
            try:
                records = mobiwork.fetch_report(cfg, rebuild_date)
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot safely rebuild {cfg.key} {anchor:%Y-%m}; "
                    f"source fetch failed for {rebuild_date}: {type(exc).__name__}: {exc}"
                ) from exc
            partitions.append((rebuild_date, records))
            if rebuild_date in target_set:
                source_rows[rebuild_date] = len(records)
        frames = build_month_from_partitions(partitions, cfg.export_mode)
    else:
        frames = read_master(existing_content, cfg.export_mode)
        for target_date in ordered_dates:
            try:
                records = mobiwork.fetch_report(cfg, target_date)
                incoming = frames_from_records(records, cfg.export_mode, target_date)
                frames = merge_partition(frames, incoming, target_date, cfg.export_mode)
                source_rows[target_date] = len(records)
            except Exception as exc:
                errors[target_date] = f"{type(exc).__name__}: {exc}"
                LOG.exception(
                    "Target failed while same-month dates continue: report=%s date=%s",
                    cfg.key,
                    target_date,
                )

    if not source_rows:
        return {
            "path": None,
            "source_rows": source_rows,
            "errors": errors,
            "master_rows": master_row_count(frames, cfg.export_mode),
            "month_rebuilt": rebuilt,
            "rebuild_days": rebuild_days,
            "remote_folder": remote_folder,
        }

    path = write_master(frames, cfg.name, anchor)
    return {
        "path": path,
        "source_rows": source_rows,
        "errors": errors,
        "master_rows": master_row_count(frames, cfg.export_mode),
        "month_rebuilt": rebuilt,
        "rebuild_days": rebuild_days,
        "remote_folder": remote_folder,
    }


def _build_or_update_master(
    cfg: ReportConfig,
    target_date: date,
    mobiwork: MobiWorkClient,
    sharepoint: SemanticSharePointClient | None,
    drive_id: str | None,
    dry_run: bool,
) -> tuple[Any, int, int, bool, int]:
    """Backward-compatible single-date helper used by tests and local callers."""
    bundle = _build_or_update_month_group(
        cfg,
        [target_date],
        mobiwork,
        sharepoint,
        drive_id,
        dry_run,
    )
    if bundle["errors"].get(target_date):
        raise RuntimeError(bundle["errors"][target_date])
    path = bundle["path"]
    if path is None:
        raise RuntimeError(f"No workbook produced for {cfg.key} {target_date}")
    return (
        path,
        int(bundle["source_rows"].get(target_date, 0)),
        int(bundle["master_rows"]),
        bool(bundle["month_rebuilt"]),
        int(bundle["rebuild_days"]),
    )


def run_incremental_all_reports(
    reports: list[ReportConfig],
    mobiwork: MobiWorkClient,
    sharepoint: SemanticSharePointClient | None,
    drive_id: str | None,
    lookback_days: int,
    dry_run: bool,
    manifest: dict[str, Any],
    sync_scope: str = "lookback",
) -> list[dict[str, Any]]:
    """Run reports independently while batching physical I/O by report and month."""
    target_dates = incremental_target_dates(sync_scope, lookback_days)
    month_groups = group_target_dates_by_month(target_dates)
    results: list[dict[str, Any]] = []
    result_map: dict[tuple[str, date], dict[str, Any]] = {}

    # Preserve the historical date-major result order for manifests and summaries.
    for target_date in target_dates:
        for cfg in reports:
            result = _result_entry(cfg, target_date)
            results.append(result)
            result_map[(cfg.key, target_date)] = result

    for cfg in reports:
        for grouped_dates in month_groups:
            anchor = grouped_dates[-1]
            group_results = [result_map[(cfg.key, value)] for value in grouped_dates]
            remote_folder = f"{cfg.folder}/{anchor:%Y}/{anchor:%m}"
            group_id = f"{cfg.key}:{anchor:%Y-%m}"
            path = None

            LOG.info(
                "Processing report/month batch report=%s month=%s targets=%s",
                cfg.key,
                anchor.strftime("%Y-%m"),
                ",".join(value.isoformat() for value in grouped_dates),
            )

            try:
                bundle = _build_or_update_month_group(
                    cfg,
                    grouped_dates,
                    mobiwork,
                    sharepoint,
                    drive_id,
                    dry_run,
                )
                path = bundle["path"]
                source_rows: dict[date, int] = bundle["source_rows"]
                target_errors: dict[date, str] = bundle["errors"]
                master_rows = int(bundle["master_rows"])
                rebuilt = bool(bundle["month_rebuilt"])
                rebuild_days = int(bundle["rebuild_days"])

                for target_date in grouped_dates:
                    result = result_map[(cfg.key, target_date)]
                    result["publish_group"] = group_id
                    result["remote_folder"] = remote_folder
                    result["master_rows"] = master_rows
                    result["month_rebuilt"] = rebuilt
                    result["rebuild_days"] = rebuild_days
                    if target_date in target_errors:
                        result["status"] = "failed"
                        result["error"] = target_errors[target_date]
                    elif target_date in source_rows:
                        result["source_rows"] = source_rows[target_date]
                    else:
                        result["status"] = "failed"
                        result["error"] = "No source result was produced for target date"

                successful_dates = [
                    value
                    for value in grouped_dates
                    if result_map[(cfg.key, value)]["status"] == "running"
                ]
                if not successful_dates or path is None:
                    for value in successful_dates:
                        result_map[(cfg.key, value)]["status"] = "failed"
                        result_map[(cfg.key, value)]["error"] = "No monthly workbook was produced"
                    continue

                for value in successful_dates:
                    result = result_map[(cfg.key, value)]
                    result["filename"] = path.name
                    result["local_size_bytes"] = path.stat().st_size

                uploaded: dict[str, Any] | None = None
                if not dry_run:
                    if not sharepoint or not drive_id:
                        raise RuntimeError("SharePoint client is unavailable in production mode")
                    uploaded = sharepoint.upload_file(drive_id, path, remote_folder)
                    upload_skipped = bool(uploaded.get("upload_skipped", False))
                    verification_mode = uploaded.get("verification_mode")
                    LOG.info(
                        "Published monthly master report=%s targets=%s verification=%s skipped=%s",
                        cfg.key,
                        len(successful_dates),
                        verification_mode or "standard",
                        upload_skipped,
                    )
                    for value in successful_dates:
                        result = result_map[(cfg.key, value)]
                        result["remote_size_bytes"] = uploaded.get("size")
                        result["verification_mode"] = verification_mode
                        result["semantic_match"] = uploaded.get("semantic_match")
                        result["upload_skipped"] = upload_skipped
                        result["web_url"] = uploaded.get("webUrl")

                    deleted = _cleanup_legacy_files(
                        sharepoint,
                        drive_id,
                        remote_folder,
                        cfg.name,
                        path.name,
                    )
                    for value in successful_dates:
                        result = result_map[(cfg.key, value)]
                        result["cleanup_deleted_count"] = len(deleted)
                        if deleted:
                            result["cleanup_deleted_files"] = deleted

                _record_monthly_export(
                    manifest,
                    cfg,
                    path,
                    sum(int(source_rows[value]) for value in successful_dates),
                    master_rows,
                    remote_folder,
                    uploaded,
                    target_dates=successful_dates,
                )
                for value in successful_dates:
                    result_map[(cfg.key, value)]["status"] = "success"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                for result in group_results:
                    if result.get("status") == "running":
                        result["status"] = "failed"
                        result["error"] = error
                        if path is not None:
                            result.setdefault("filename", path.name)
                            result.setdefault("local_size_bytes", path.stat().st_size)
                LOG.exception(
                    "Report/month batch failed but remaining batches continue: report=%s month=%s",
                    cfg.key,
                    anchor.strftime("%Y-%m"),
                )

    manifest["report_results"] = results
    return results


def _finalize_manifest(
    manifest: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    failed = [item for item in results if item.get("status") == "failed"]
    successful = [item for item in results if item.get("status") == "success"]
    files = manifest.get("files", [])
    dry_run = bool(manifest.get("dry_run", False))
    upload_skipped_count = sum(bool(item.get("upload_skipped", False)) for item in files)

    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    manifest["file_count"] = len(files)
    manifest["workbook_group_count"] = len(files)
    manifest["target_execution_count"] = len(results)
    manifest["source_row_count"] = sum(int(item.get("source_rows", 0)) for item in files)
    manifest["master_row_count"] = sum(int(item.get("master_rows", 0)) for item in files)
    manifest["upload_skipped_count"] = upload_skipped_count
    manifest["sharepoint_write_avoided_count"] = upload_skipped_count
    manifest["sharepoint_write_count"] = (
        0 if dry_run else max(len(files) - upload_skipped_count, 0)
    )
    manifest["successful_report_count"] = len(successful)
    manifest["failed_report_count"] = len(failed)
    manifest["status"] = "success" if not failed else "partial_failure"
    if failed:
        manifest["error"] = "; ".join(
            f"{item['report']}[{item['target_date']}]: {item.get('error', 'failed')}"
            for item in failed
        )[:4000]


def run_incremental() -> dict[str, Any]:
    dry_run = _env_bool("DRY_RUN", False)
    lookback_days = int(os.environ.get("LOOKBACK_DAYS", "1"))
    sync_scope = os.environ.get("SYNC_SCOPE", "yesterday").strip().casefold()
    reports = core.enabled_reports()
    manifest = core._new_manifest("incremental", dry_run)
    manifest["reports"] = [cfg.key for cfg in reports]
    manifest["sync_scope"] = sync_scope
    manifest["storage_model"] = "single_monthly_master_per_report"
    manifest["execution_policy"] = "continue_on_report_error"
    manifest["xlsx_verification"] = "semantic_cell_content"
    manifest["publish_batching"] = "one_read_one_publish_per_report_month"

    sharepoint: SemanticSharePointClient | None = None
    drive_id: str | None = None

    try:
        mobiwork, sharepoint, drive_id = build_clients(dry_run)
        results = run_incremental_all_reports(
            reports,
            mobiwork,
            sharepoint,
            drive_id,
            lookback_days,
            dry_run,
            manifest,
            sync_scope=sync_scope,
        )
        _finalize_manifest(manifest, results)
        core._write_manifest(manifest)
        try:
            core._upload_manifest(manifest, sharepoint, drive_id)
        except Exception as exc:
            manifest["audit_upload_error"] = f"{type(exc).__name__}: {exc}"
            core._write_manifest(manifest)
            LOG.exception("Unable to upload audit manifest")

        if manifest["failed_report_count"]:
            raise RuntimeError(
                f"{manifest['failed_report_count']} report execution(s) failed after "
                f"{manifest['successful_report_count']} succeeded; see sync_manifest.json"
            )
        return manifest
    except Exception:
        if manifest.get("status") == "running":
            manifest["status"] = "failed"
            manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
            core._write_manifest(manifest)
            try:
                core._upload_manifest(manifest, sharepoint, drive_id)
            except Exception:
                LOG.exception("Unable to upload failure audit manifest")
        raise


def run() -> dict[str, Any]:
    return run_incremental()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run()
