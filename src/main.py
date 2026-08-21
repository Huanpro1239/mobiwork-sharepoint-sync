from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from excel_export import export_excel
from mobiwork import MobiWorkClient, ReportConfig
from sharepoint import SharePointClient


LOG = logging.getLogger("mobiwork_sync")


def load_reports(path: Path) -> list[ReportConfig]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [ReportConfig(**item) for item in payload.get("reports", [])]


def target_dates(lookback_days: int) -> list[date]:
    if lookback_days < 1 or lookback_days > 31:
        raise ValueError("lookback_days must be between 1 and 31")
    today_vn = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
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
    site_id = sharepoint.get_site_id()
    drive_id = sharepoint.get_drive_id(site_id)
    return mobiwork, sharepoint, drive_id


def enabled_reports() -> list[ReportConfig]:
    reports = [cfg for cfg in load_reports(Path("config/reports.json")) if cfg.enabled]
    if not reports:
        raise RuntimeError(
            "No MobiWork reports are enabled. Map Swagger endpoints in config/reports.json first."
        )
    return reports


def run_incremental(lookback_days: int, dry_run: bool) -> None:
    """Daily mode: export one file per day, normally D-1 only."""
    reports = enabled_reports()
    mobiwork, sharepoint, drive_id = build_clients(dry_run)

    for target_date in target_dates(lookback_days):
        LOG.info("Incremental sync date: %s", target_date)
        for cfg in reports:
            LOG.info("Fetching report=%s", cfg.key)
            records = mobiwork.fetch_report(cfg, target_date)
            path = export_excel(records, cfg.name, target_date, cfg.export_mode)
            LOG.info("Exported %s source rows -> %s", len(records), path)

            if dry_run:
                continue

            remote_folder = f"{cfg.folder}/{target_date:%Y}/{target_date:%m}"
            uploaded = sharepoint.upload_file(drive_id, path, remote_folder)
            LOG.info("Uploaded -> %s", uploaded.get("webUrl", remote_folder))


def run_bootstrap(
    dry_run: bool,
    empty_month_stop: int,
    floor_date: date,
) -> None:
    """One-time history load: walk backward month by month from yesterday.

    The scan stops after `empty_month_stop` consecutive months where all enabled
    reports return zero rows, or when `floor_date` is reached. This avoids asking
    the user to know the oldest MobiWork date in advance while still protecting
    against isolated empty months inside the historical period.
    """
    if empty_month_stop < 1 or empty_month_stop > 120:
        raise ValueError("BOOTSTRAP_EMPTY_MONTHS must be between 1 and 120")

    reports = enabled_reports()
    mobiwork, sharepoint, drive_id = build_clients(dry_run)
    yesterday = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date() - timedelta(days=1)

    cursor_end = yesterday
    consecutive_empty_months = 0
    partition_count = 0

    LOG.info(
        "Bootstrap history: start=%s, direction=newest-to-oldest, floor=%s, "
        "stop_after_empty_months=%s",
        yesterday,
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
                LOG.info("No rows for report=%s in %s..%s; skipping empty file", cfg.key, from_date, to_date)
                continue

            path = export_excel(
                records,
                cfg.name,
                from_date,
                cfg.export_mode,
                file_suffix=file_suffix,
            )
            LOG.info("Exported %s source rows -> %s", len(records), path)

            if dry_run:
                continue

            remote_folder = f"{cfg.folder}/{from_date:%Y}/{from_date:%m}"
            uploaded = sharepoint.upload_file(drive_id, path, remote_folder)
            LOG.info("Uploaded -> %s", uploaded.get("webUrl", remote_folder))

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

        if consecutive_empty_months >= empty_month_stop:
            LOG.info(
                "Bootstrap stop condition reached after %s consecutive empty months. "
                "Historical scan complete.",
                consecutive_empty_months,
            )
            break

        cursor_end = month_start - timedelta(days=1)
    else:
        LOG.info("Bootstrap reached hard floor date %s", floor_date)


def run(
    sync_mode: str,
    lookback_days: int,
    dry_run: bool,
    bootstrap_empty_months: int,
    bootstrap_floor_date: str,
) -> None:
    mode = sync_mode.strip().lower()
    if mode == "incremental":
        run_incremental(lookback_days, dry_run)
        return

    if mode == "bootstrap":
        floor_date = parse_iso_date(bootstrap_floor_date, "BOOTSTRAP_FLOOR_DATE")
        run_bootstrap(dry_run, bootstrap_empty_months, floor_date)
        return

    raise ValueError("SYNC_MODE must be incremental or bootstrap")


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
        default=int(os.environ.get("LOOKBACK_DAYS", "1")),
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
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "false").lower() == "true",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    args = parse_args()
    run(
        args.sync_mode,
        args.lookback_days,
        args.dry_run,
        args.bootstrap_empty_months,
        args.bootstrap_floor_date,
    )
