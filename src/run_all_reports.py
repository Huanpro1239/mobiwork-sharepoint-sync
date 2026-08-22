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
) -> None:
    """Record current-source rows separately from rows stored in the monthly master."""
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
    if uploaded:
        export["verification_mode"] = uploaded.get("verification_mode")
        export["semantic_match"] = uploaded.get("semantic_match")


def _build_or_update_master(
    cfg: ReportConfig,
    target_date: date,
    mobiwork: MobiWorkClient,
    sharepoint: SemanticSharePointClient | None,
    drive_id: str | None,
    dry_run: bool,
) -> tuple[Any, int, int, bool, int]:
    """Return path, target-day rows, master rows, rebuilt flag, rebuild days."""
    remote_folder = f"{cfg.folder}/{target_date:%Y}/{target_date:%m}"
    canonical_name = master_filename(cfg.name, target_date)
    remote_path = f"{remote_folder}/{canonical_name}"

    if dry_run:
        records = mobiwork.fetch_report(cfg, target_date)
        frames = build_month_from_partitions([(target_date, records)], cfg.export_mode)
        path = write_master(frames, cfg.name, target_date)
        return path, len(records), master_row_count(frames, cfg.export_mode), False, 0

    if not sharepoint or not drive_id:
        raise RuntimeError("SharePoint client is unavailable in production mode")

    existing_content = sharepoint.download_file_bytes(drive_id, remote_path)
    if existing_content is None:
        rebuild_dates = month_dates_through(target_date)
        partitions: list[tuple[date, list[dict[str, Any]]]] = []
        target_rows = 0
        LOG.info(
            "Monthly master missing; rebuilding report=%s month=%s days=%s",
            cfg.key,
            target_date.strftime("%Y-%m"),
            len(rebuild_dates),
        )
        for rebuild_date in rebuild_dates:
            records = mobiwork.fetch_report(cfg, rebuild_date)
            partitions.append((rebuild_date, records))
            if rebuild_date == target_date:
                target_rows = len(records)
        frames = build_month_from_partitions(partitions, cfg.export_mode)
        path = write_master(frames, cfg.name, target_date)
        return (
            path,
            target_rows,
            master_row_count(frames, cfg.export_mode),
            True,
            len(rebuild_dates),
        )

    existing_frames = read_master(existing_content, cfg.export_mode)
    records = mobiwork.fetch_report(cfg, target_date)
    incoming = frames_from_records(records, cfg.export_mode, target_date)
    merged = merge_partition(existing_frames, incoming, target_date, cfg.export_mode)
    path = write_master(merged, cfg.name, target_date)
    return path, len(records), master_row_count(merged, cfg.export_mode), False, 0


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
    """Run every report independently; one failure must not block the others."""
    results: list[dict[str, Any]] = []

    for target_date in incremental_target_dates(sync_scope, lookback_days):
        LOG.info("Incremental sync date: %s scope=%s", target_date, sync_scope)
        for cfg in reports:
            result = _result_entry(cfg, target_date)
            results.append(result)
            path = None
            remote_folder = f"{cfg.folder}/{target_date:%Y}/{target_date:%m}"

            try:
                path, target_rows, master_rows, rebuilt, rebuild_days = _build_or_update_master(
                    cfg,
                    target_date,
                    mobiwork,
                    sharepoint,
                    drive_id,
                    dry_run,
                )
                result["source_rows"] = target_rows
                result["master_rows"] = master_rows
                result["month_rebuilt"] = rebuilt
                result["rebuild_days"] = rebuild_days
                result["filename"] = path.name
                result["local_size_bytes"] = path.stat().st_size
                result["remote_folder"] = remote_folder
                LOG.info(
                    "Prepared monthly master report=%s target_rows=%s master_rows=%s file=%s",
                    cfg.key,
                    target_rows,
                    master_rows,
                    path,
                )

                uploaded: dict[str, Any] | None = None
                if not dry_run:
                    if not sharepoint or not drive_id:
                        raise RuntimeError(
                            "SharePoint client is unavailable in production mode"
                        )
                    uploaded = sharepoint.upload_file(drive_id, path, remote_folder)
                    result["remote_size_bytes"] = uploaded.get("size")
                    result["verification_mode"] = uploaded.get("verification_mode")
                    result["semantic_match"] = uploaded.get("semantic_match")
                    result["web_url"] = uploaded.get("webUrl")
                    LOG.info(
                        "Uploaded monthly master report=%s -> %s verification=%s",
                        cfg.key,
                        uploaded.get("webUrl", remote_folder),
                        uploaded.get("verification_mode", "standard"),
                    )

                    deleted = _cleanup_legacy_files(
                        sharepoint,
                        drive_id,
                        remote_folder,
                        cfg.name,
                        path.name,
                    )
                    result["cleanup_deleted_count"] = len(deleted)
                    if deleted:
                        result["cleanup_deleted_files"] = deleted

                _record_monthly_export(
                    manifest,
                    cfg,
                    path,
                    target_rows,
                    master_rows,
                    remote_folder,
                    uploaded,
                )
                result["status"] = "success"
            except Exception as exc:
                result["status"] = "failed"
                result["error"] = f"{type(exc).__name__}: {exc}"
                if path is not None:
                    result.setdefault("filename", path.name)
                    result.setdefault("local_size_bytes", path.stat().st_size)
                LOG.exception(
                    "Report failed but remaining reports will continue: report=%s date=%s",
                    cfg.key,
                    target_date,
                )

    manifest["report_results"] = results
    return results


def _finalize_manifest(
    manifest: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    failed = [item for item in results if item.get("status") == "failed"]
    successful = [item for item in results if item.get("status") == "success"]
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    manifest["file_count"] = len(manifest.get("files", []))
    manifest["source_row_count"] = sum(
        int(item.get("source_rows", 0)) for item in manifest.get("files", [])
    )
    manifest["master_row_count"] = sum(
        int(item.get("master_rows", 0)) for item in manifest.get("files", [])
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
