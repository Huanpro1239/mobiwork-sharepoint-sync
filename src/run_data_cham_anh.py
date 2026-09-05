from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

import main as core
from data_cham_anh_export import publish_data_cham_anh_month
from mobiwork import ReportConfig
from run_all_reports import incremental_target_dates
from sharepoint_semantic import SemanticSharePointClient


LOG = logging.getLogger("mobiwork_sync")


def month_anchors(target_dates: list[date]) -> list[date]:
    """Return the latest requested date for each calendar month in input order."""
    anchors: dict[tuple[int, int], date] = {}
    for target_date in target_dates:
        key = (target_date.year, target_date.month)
        current = anchors.get(key)
        if current is None or target_date > current:
            anchors[key] = target_date
    return list(anchors.values())


def publish_target_months(
    reports: list[ReportConfig],
    sharepoint: Any,
    drive_id: str | None,
    target_dates: list[date],
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Publish one combined workbook for each touched month after report sync succeeds."""
    if dry_run:
        return []
    if sharepoint is None or not drive_id:
        raise RuntimeError("SharePoint client and drive_id are required for Data cham anh publish")

    exports: list[dict[str, Any]] = []
    for anchor in month_anchors(target_dates):
        result = publish_data_cham_anh_month(
            reports,
            sharepoint,
            drive_id,
            anchor,
        )
        exports.append(result)
        LOG.info(
            "Published Data cham anh workbook month=%s image_rows=%s bill_rows=%s path=%s/%s",
            result.get("month", anchor.strftime("%Y-%m")),
            result.get("data_anh_rows", 0),
            result.get("data_don_hang_rows", 0),
            result.get("remote_folder", ""),
            result.get("filename", ""),
        )
    return exports


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def run() -> list[dict[str, Any]]:
    dry_run = _env_bool("DRY_RUN", False)
    if dry_run:
        LOG.info("Skipping Data cham anh SharePoint publish in dry-run mode")
        return []

    sync_scope = os.environ.get("SYNC_SCOPE", "yesterday").strip().casefold()
    lookback_days = int(os.environ.get("LOOKBACK_DAYS", "1"))
    target_dates = incremental_target_dates(sync_scope, lookback_days)
    reports = core.enabled_reports()

    sharepoint = SemanticSharePointClient.from_env()
    drive_id = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
    if not drive_id:
        site_id = sharepoint.get_site_id()
        drive_id = sharepoint.get_drive_id(site_id)

    return publish_target_months(
        reports,
        sharepoint,
        drive_id,
        target_dates,
        dry_run=False,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run()
