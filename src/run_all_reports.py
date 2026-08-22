from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import main as core
from excel_export import export_excel
from mobiwork import MobiWorkClient, ReportConfig
from sharepoint_semantic import SemanticSharePointClient


LOG = logging.getLogger("mobiwork_sync")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def incremental_target_dates(sync_scope: str, lookback_days: int) -> list[date]:
    """Resolve incremental dates in Vietnam local time.

    today: current business day, used by the hourly near-real-time refresh.
    yesterday: previous business day, used by the 09:00 daily finalization.
    lookback: previous N days, retained for manual recovery/backfill.
    """
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
        "status": "running",
    }


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
            source_rows = 0
            remote_folder: str | None = None

            try:
                LOG.info("Fetching report=%s", cfg.key)
                records = mobiwork.fetch_report(cfg, target_date)
                source_rows = len(records)
                result["source_rows"] = source_rows

                path = export_excel(records, cfg.name, target_date, cfg.export_mode)
                result["filename"] = path.name
                result["local_size_bytes"] = path.stat().st_size
                LOG.info("Exported %s source rows -> %s", source_rows, path)

                uploaded: dict[str, Any] | None = None
                if not dry_run:
                    if not sharepoint or not drive_id:
                        raise RuntimeError(
                            "SharePoint client is unavailable in production mode"
                        )
                    remote_folder = f"{cfg.folder}/{target_date:%Y}/{target_date:%m}"
                    result["remote_folder"] = remote_folder
                    uploaded = sharepoint.upload_file(drive_id, path, remote_folder)
                    result["remote_size_bytes"] = uploaded.get("size")
                    result["verification_mode"] = uploaded.get("verification_mode")
                    result["semantic_match"] = uploaded.get("semantic_match")
                    result["web_url"] = uploaded.get("webUrl")
                    LOG.info(
                        "Uploaded report=%s -> %s verification=%s",
                        cfg.key,
                        uploaded.get("webUrl", remote_folder),
                        uploaded.get("verification_mode", "standard"),
                    )

                core._record_export(
                    manifest,
                    cfg,
                    path,
                    source_rows,
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
    mode = os.environ.get("SYNC_MODE", "incremental").strip().casefold()
    if mode == "incremental":
        return run_incremental()
    if mode == "bootstrap":
        # Reuse the existing resumable bootstrap orchestration, but inject the
        # semantic SharePoint client so Excel verification is consistent.
        core.SharePointClient = SemanticSharePointClient
        return core.run(
            "bootstrap",
            int(os.environ.get("LOOKBACK_DAYS", "1")),
            _env_bool("DRY_RUN", False),
            int(os.environ.get("BOOTSTRAP_EMPTY_MONTHS", "24")),
            os.environ.get("BOOTSTRAP_FLOOR_DATE", "2000-01-01"),
            _env_bool("RESET_BOOTSTRAP_STATE", False),
        )
    raise ValueError("SYNC_MODE must be incremental or bootstrap")


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run()
