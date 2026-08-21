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


def run(lookback_days: int, dry_run: bool) -> None:
    reports = [cfg for cfg in load_reports(Path("config/reports.json")) if cfg.enabled]
    if not reports:
        raise RuntimeError(
            "No MobiWork reports are enabled. Map Swagger endpoints in config/reports.json first."
        )

    mobiwork = MobiWorkClient.from_env()
    sharepoint = None if dry_run else SharePointClient.from_env()
    site_id = None if dry_run else sharepoint.get_site_id()
    drive_id = None if dry_run else sharepoint.get_drive_id(site_id)

    for target_date in target_dates(lookback_days):
        LOG.info("Sync date: %s", target_date)
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.environ.get("LOOKBACK_DAYS", "3")),
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
    run(args.lookback_days, args.dry_run)
