from __future__ import annotations

import argparse
import calendar
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


def month_windows(start_date: date, end_date: date) -> list[tuple[date, date]]:
    if end_date < start_date:
        raise ValueError("BACKFILL_END_DATE must be on or after BACKFILL_START_DATE")

    windows: list[tuple[date, date]] = []
    current = start_date
    while current <= end_date:
        last_day = calendar.monthrange(current.year, current.month)[1]
        natural_month_end = date(current.year, current.month, last_day)
        window_end = min(end_date, natural_month_end)
        windows.append((current, window_end))
        current = window_end + timedelta(days=1)
    return windows


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


def run_backfill(start_date: date, end_date: date, dry_run: bool) -> None:
    reports = enabled_reports()
    mobiwork, sharepoint, drive_id = build_clients(dry_run)
    windows = month_windows(start_date, end_date)

    LOG.info(
        "Historical backfill: %s -> %s (%s monthly partitions)",
        start_date,
        end_date,
        len(windows),
    )

    for from_date, to_date in windows:
        LOG.info("Backfill partition: %s -> %s", from_date, to_date)
        file_suffix = f"History_{from_date:%Y-%m-%d}_to_{to_date:%Y-%m-%d}"

        for cfg in reports:
            LOG.info("Fetching report=%s range=%s..%s", cfg.key, from_date, to_date)
            records = mobiwork.fetch_report_range(cfg, from_date, to_date)
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


def run(
    sync_mode: str,
    lookback_days: int,
    dry_run: bool,
    backfill_start_date: str = "",
    backfill_end_date: str = "",
) -> None:
    mode = sync_mode.strip().lower()
    if mode == "incremental":
        run_incremental(lookback_days, dry_run)
        return

    if mode == "backfill":
        if not backfill_start_date or not backfill_end_date:
            raise ValueError(
                "BACKFILL_START_DATE and BACKFILL_END_DATE are required in backfill mode"
            )
        start_date = parse_iso_date(backfill_start_date, "BACKFILL_START_DATE")
        end_date = parse_iso_date(backfill_end_date, "BACKFILL_END_DATE")
        run_backfill(start_date, end_date, dry_run)
        return

    raise ValueError("SYNC_MODE must be incremental or backfill")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sync-mode",
        default=os.environ.get("SYNC_MODE", "incremental"),
        choices=("incremental", "backfill"),
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.environ.get("LOOKBACK_DAYS", "3")),
    )
    parser.add_argument(
        "--backfill-start-date",
        default=os.environ.get("BACKFILL_START_DATE", ""),
    )
    parser.add_argument(
        "--backfill-end-date",
        default=os.environ.get("BACKFILL_END_DATE", ""),
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
        args.backfill_start_date,
        args.backfill_end_date,
    )
