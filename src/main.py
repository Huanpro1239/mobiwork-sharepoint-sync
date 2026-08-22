from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from excel_export import export_excel
from mobiwork import MobiWorkClient, ReportConfig
from sharepoint import SharePointClient


LOG = logging.getLogger("mobiwork_sync")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
BOOTSTRAP_STATE_PATH = "_sync_state/bootstrap.json"


def load_reports(path: Path) -> list[ReportConfig]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    reports = payload.get("reports", [])
    if not isinstance(reports, list):
        raise TypeError("config/reports.json must contain a reports array")
    return [ReportConfig(**item) for item in reports]


def target_dates(lookback_days: int) -> list[date]:
    if lookback_days < 1 or lookback_days > 31:
        raise ValueError("lookback_days must be between 1 and 31")
    today_vn = datetime.now(VN_TZ).date()
    return [today_vn - timedelta(days=offset) for offset in range(1, lookback_days + 1)]


def parse_iso_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must use YYYY-MM-DD format") from exc


def build_clients(dry_run: bool) -> tuple[MobiWorkClient, SharePointClient | None, str | None]:
    mobiwork = MobiWorkClient.from_env()
    if dry_run:
        return mobiwork, None, None

    sharepoint = SharePointClient.from_env()
    drive_id = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
    if not drive_id:
        site_id = sharepoint.get_site_id()
        drive_id = sharepoint.get_drive_id(site_id)
    return mobiwork, sharepoint, drive_id


def enabled_reports() -> list[ReportConfig]:
    reports = [cfg for cfg in load_reports(Path("config/reports.json")) if cfg.enabled]
    if not reports:
        raise RuntimeError("No MobiWork reports are enabled in config/reports.json")
    return reports


def _new_manifest(mode: str, dry_run: bool) -> dict[str, Any]:
    now_vn = datetime.now(VN_TZ)
    github_run = os.environ.get("GITHUB_RUN_ID", "local")
    github_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    run_id = f"{now_vn:%Y%m%dT%H%M%S}_{github_run}_{github_attempt}"
    return {
        "run_id": run_id,
        "mode": mode,
        "dry_run": dry_run,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "timezone": "Asia/Ho_Chi_Minh",
        "files": [],
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_export(
    manifest: dict[str, Any],
    cfg: ReportConfig,
    path: Path,
    source_rows: int,
    remote_folder: str | None,
    uploaded: dict[str, Any] | None,
) -> None:
    manifest["files"].append(
        {
            "report": cfg.key,
            "report_name": cfg.name,
            "source_rows": source_rows,
            "filename": path.name,
            "local_size_bytes": path.stat().st_size,
            "sha256": _file_sha256(path),
            "remote_folder": remote_folder,
            "remote_size_bytes": uploaded.get("size") if uploaded else None,
            "web_url": uploaded.get("webUrl") if uploaded else None,
        }
    )


def _write_manifest(manifest: dict[str, Any]) -> Path:
    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "sync_manifest.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def _upload_manifest(
    manifest: dict[str, Any],
    sharepoint: SharePointClient | None,
    drive_id: str | None,
) -> None:
    if not sharepoint or not drive_id:
        return
    now_vn = datetime.now(VN_TZ)
    remote_path = f"_sync_runs/{now_vn:%Y}/{now_vn:%m}/{manifest['run_id']}.json"
    sharepoint.upload_json(drive_id, remote_path, manifest)
    LOG.info("Uploaded audit manifest -> %s", remote_path)


def run_incremental(
    reports: list[ReportConfig],
    mobiwork: MobiWorkClient,
    sharepoint: SharePointClient | None,
    drive_id: str | None,
    lookback_days: int,
    dry_run: bool,
    manifest: dict[str, Any],
) -> None:
    """Daily mode: refresh deterministic daily files so reruns are idempotent."""
    for target_date in target_dates(lookback_days):
        LOG.info("Incremental sync date: %s", target_date)
        for cfg in reports:
            LOG.info("Fetching report=%s", cfg.key)
            records = mobiwork.fetch_report(cfg, target_date)
            path = export_excel(records, cfg.name, target_date, cfg.export_mode)
            LOG.info("Exported %s source rows -> %s", len(records), path)

            remote_folder: str | None = None
            uploaded: dict[str, Any] | None = None
            if not dry_run:
                if not sharepoint or not drive_id:
                    raise RuntimeError("SharePoint client is unavailable in production mode")
                remote_folder = f"{cfg.folder}/{target_date:%Y}/{target_date:%m}"
                uploaded = sharepoint.upload_file(drive_id, path, remote_folder)
                LOG.info("Uploaded -> %s", uploaded.get("webUrl", remote_folder))

            _record_export(
                manifest,
                cfg,
                path,
                len(records),
                remote_folder,
                uploaded,
            )


def _bootstrap_signature(
    reports: list[ReportConfig], floor_date: date, empty_month_stop: int
) -> dict[str, Any]:
    return {
        "report_keys": sorted(cfg.key for cfg in reports),
        "floor_date": floor_date.isoformat(),
        "empty_month_stop": empty_month_stop,
    }


def _load_bootstrap_state(
    sharepoint: SharePointClient | None,
    drive_id: str | None,
    signature: dict[str, Any],
    reset: bool,
) -> dict[str, Any] | None:
    if reset or not sharepoint or not drive_id:
        return None
    state = sharepoint.download_json(drive_id, BOOTSTRAP_STATE_PATH)
    if not state:
        return None
    if state.get("signature") != signature:
        LOG.warning("Bootstrap checkpoint configuration changed; starting a new history scan")
        return None
    return state


def _save_bootstrap_state(
    sharepoint: SharePointClient | None,
    drive_id: str | None,
    signature: dict[str, Any],
    next_cursor_end: date,
    consecutive_empty_months: int,
    completed: bool,
) -> None:
    if not sharepoint or not drive_id:
        return
    payload = {
        "signature": signature,
        "next_cursor_end": next_cursor_end.isoformat(),
        "consecutive_empty_months": consecutive_empty_months,
        "completed": completed,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    sharepoint.upload_json(drive_id, BOOTSTRAP_STATE_PATH, payload)


def run_bootstrap(
    reports: list[ReportConfig],
    mobiwork: MobiWorkClient,
    sharepoint: SharePointClient | None,
    drive_id: str | None,
    dry_run: bool,
    empty_month_stop: int,
    floor_date: date,
    reset_state: bool,
    manifest: dict[str, Any],
) -> None:
    """One-time history load, resumable month-by-month using a SharePoint checkpoint."""
    if empty_month_stop < 1 or empty_month_stop > 120:
        raise ValueError("BOOTSTRAP_EMPTY_MONTHS must be between 1 and 120")

    yesterday = datetime.now(VN_TZ).date() - timedelta(days=1)
    signature = _bootstrap_signature(reports, floor_date, empty_month_stop)
    state = _load_bootstrap_state(sharepoint, drive_id, signature, reset_state)

    if state and state.get("completed") is True:
        LOG.info(
            "Bootstrap checkpoint is already complete. Use --reset-bootstrap-state "
            "to intentionally rescan history."
        )
        manifest["bootstrap"] = {"checkpoint": "already_complete"}
        return

    cursor_end = yesterday
    consecutive_empty_months = 0
    if state:
        cursor_end = parse_iso_date(str(state["next_cursor_end"]), "next_cursor_end")
        consecutive_empty_months = int(state.get("consecutive_empty_months", 0))
        LOG.info("Resuming bootstrap from checkpoint: %s", cursor_end)

    manifest["bootstrap"] = {
        "floor_date": floor_date.isoformat(),
        "empty_month_stop": empty_month_stop,
        "resumed": bool(state),
    }

    partition_count = 0
    LOG.info(
        "Bootstrap history: cursor=%s, direction=newest-to-oldest, floor=%s, "
        "stop_after_empty_months=%s",
        cursor_end,
        floor_date,
        empty_month_stop,
    )

    while cursor_end >= floor_date:
        month_start = cursor_end.replace(day=1)
        from_date = max(month_start, floor_date)
        to_date = cursor_end
        partition_count += 1
        month_total_rows = 0

        LOG.info("Bootstrap partition #%s: %s -> %s", partition_count, from_date, to_date)
        file_suffix = f"History_{from_date:%Y-%m-%d}_to_{to_date:%Y-%m-%d}"

        for cfg in reports:
            LOG.info("Fetching report=%s range=%s..%s", cfg.key, from_date, to_date)
            records = mobiwork.fetch_report_range(cfg, from_date, to_date)
            month_total_rows += len(records)

            if not records:
                LOG.info(
                    "No rows for report=%s in %s..%s; skipping empty history file",
                    cfg.key,
                    from_date,
                    to_date,
                )
                continue

            path = export_excel(
                records,
                cfg.name,
                from_date,
                cfg.export_mode,
                file_suffix=file_suffix,
            )
            LOG.info("Exported %s source rows -> %s", len(records), path)

            remote_folder: str | None = None
            uploaded: dict[str, Any] | None = None
            if not dry_run:
                if not sharepoint or not drive_id:
                    raise RuntimeError("SharePoint client is unavailable in production mode")
                remote_folder = f"{cfg.folder}/{from_date:%Y}/{from_date:%m}"
                uploaded = sharepoint.upload_file(drive_id, path, remote_folder)
                LOG.info("Uploaded -> %s", uploaded.get("webUrl", remote_folder))

            _record_export(
                manifest,
                cfg,
                path,
                len(records),
                remote_folder,
                uploaded,
            )

        if month_total_rows == 0:
            consecutive_empty_months += 1
            LOG.info(
                "All reports empty for this month. Consecutive empty months: %s/%s",
                consecutive_empty_months,
                empty_month_stop,
            )
        else:
            consecutive_empty_months = 0
            LOG.info("Partition contains %s source rows across all reports", month_total_rows)

        next_cursor_end = month_start - timedelta(days=1)
        completed = consecutive_empty_months >= empty_month_stop or next_cursor_end < floor_date
        if not dry_run:
            _save_bootstrap_state(
                sharepoint,
                drive_id,
                signature,
                next_cursor_end,
                consecutive_empty_months,
                completed,
            )

        if consecutive_empty_months >= empty_month_stop:
            LOG.info(
                "Bootstrap stop condition reached after %s consecutive empty months",
                consecutive_empty_months,
            )
            break

        cursor_end = next_cursor_end

    manifest["bootstrap"]["partitions_processed"] = partition_count


def run(
    sync_mode: str,
    lookback_days: int,
    dry_run: bool,
    bootstrap_empty_months: int,
    bootstrap_floor_date: str,
    reset_bootstrap_state: bool,
) -> dict[str, Any]:
    mode = sync_mode.strip().lower()
    if mode not in {"incremental", "bootstrap"}:
        raise ValueError("SYNC_MODE must be incremental or bootstrap")

    manifest = _new_manifest(mode, dry_run)
    sharepoint: SharePointClient | None = None
    drive_id: str | None = None

    try:
        reports = enabled_reports()
        manifest["reports"] = [cfg.key for cfg in reports]
        mobiwork, sharepoint, drive_id = build_clients(dry_run)

        if mode == "incremental":
            run_incremental(
                reports,
                mobiwork,
                sharepoint,
                drive_id,
                lookback_days,
                dry_run,
                manifest,
            )
        else:
            floor_date = parse_iso_date(bootstrap_floor_date, "BOOTSTRAP_FLOOR_DATE")
            run_bootstrap(
                reports,
                mobiwork,
                sharepoint,
                drive_id,
                dry_run,
                bootstrap_empty_months,
                floor_date,
                reset_bootstrap_state,
                manifest,
            )

        manifest["status"] = "success"
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        manifest["file_count"] = len(manifest["files"])
        manifest["source_row_count"] = sum(
            int(item["source_rows"]) for item in manifest["files"]
        )
        _write_manifest(manifest)
        _upload_manifest(manifest, sharepoint, drive_id)
        return manifest
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        _write_manifest(manifest)
        try:
            _upload_manifest(manifest, sharepoint, drive_id)
        except Exception:
            LOG.exception("Unable to upload failure audit manifest")
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sync-mode",
        default=os.environ.get("SYNC_MODE", "incremental"),
        choices=("incremental", "bootstrap"),
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.environ.get("LOOKBACK_DAYS", "3")),
    )
    parser.add_argument(
        "--bootstrap-empty-months",
        type=int,
        default=int(os.environ.get("BOOTSTRAP_EMPTY_MONTHS", "24")),
    )
    parser.add_argument(
        "--bootstrap-floor-date",
        default=os.environ.get("BOOTSTRAP_FLOOR_DATE", "2000-01-01"),
    )
    parser.add_argument(
        "--reset-bootstrap-state",
        action="store_true",
        default=os.environ.get("RESET_BOOTSTRAP_STATE", "false").lower() == "true",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "false").lower() == "true",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    args = parse_args()
    run(
        args.sync_mode,
        args.lookback_days,
        args.dry_run,
        args.bootstrap_empty_months,
        args.bootstrap_floor_date,
        args.reset_bootstrap_state,
    )
