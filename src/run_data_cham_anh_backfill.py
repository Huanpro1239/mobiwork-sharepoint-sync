from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import main as core
from data_cham_anh_export import DEFAULT_ROOT_FOLDER, publish_data_cham_anh_month
from mobiwork import ReportConfig
from sharepoint_semantic import SemanticSharePointClient


LOG = logging.getLogger("mobiwork_sync")
DEFAULT_MAX_MONTHS = 24
DEFAULT_OUTPUT_DIR = Path("output/backfill")
DEFAULT_MANIFEST_PATH = Path("output/data_cham_anh_backfill_manifest.json")


def parse_month(value: str, *, label: str) -> date:
    normalized = value.strip()
    try:
        parsed = datetime.strptime(normalized, "%Y-%m")
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM format, got {value!r}") from exc
    if parsed.strftime("%Y-%m") != normalized:
        raise ValueError(f"{label} must use YYYY-MM format, got {value!r}")
    return date(parsed.year, parsed.month, 1)


def month_range(start: date, end: date, *, max_months: int = DEFAULT_MAX_MONTHS) -> list[date]:
    if max_months <= 0:
        raise ValueError("max_months must be positive")
    if end < start:
        raise ValueError("DATA_CHAM_ANH_TO_MONTH must be the same as or after DATA_CHAM_ANH_FROM_MONTH")

    months: list[date] = []
    current = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while current <= last:
        months.append(current)
        if len(months) > max_months:
            raise ValueError(
                f"Requested Data cham anh backfill spans more than {max_months} months; "
                "split it into smaller runs"
            )
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return months


def publish_backfill(
    reports: list[ReportConfig],
    sharepoint: Any,
    drive_id: str,
    months: list[date],
    *,
    root_folder: str = DEFAULT_ROOT_FOLDER,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    failures = 0

    for anchor in months:
        month_label = anchor.strftime("%Y-%m")
        try:
            result = publish_data_cham_anh_month(
                reports,
                sharepoint,
                drive_id,
                anchor,
                root_folder=root_folder,
                output_dir=output_dir,
            )
            results.append(result)
            LOG.info(
                "Backfilled Data cham anh month=%s image_rows=%s bill_rows=%s path=%s/%s",
                month_label,
                result.get("data_anh_rows", 0),
                result.get("data_don_hang_rows", 0),
                result.get("remote_folder", ""),
                result.get("filename", ""),
            )
        except Exception as exc:  # Continue so one missing month does not hide the rest of the range.
            failures += 1
            LOG.exception("Data cham anh backfill failed for month=%s", month_label)
            results.append(
                {
                    "status": "failed",
                    "month": month_label,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": "failed" if failures else "success",
        "requested_month_count": len(months),
        "successful_month_count": len(months) - failures,
        "failed_month_count": failures,
        "root_folder": root_folder,
        "results": results,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if failures:
        raise RuntimeError(f"Data cham anh backfill failed for {failures}/{len(months)} month(s)")
    return results


def run() -> list[dict[str, Any]]:
    today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
    current_month = today.strftime("%Y-%m")
    from_value = os.environ.get("DATA_CHAM_ANH_FROM_MONTH", "").strip() or current_month
    to_value = os.environ.get("DATA_CHAM_ANH_TO_MONTH", "").strip() or from_value
    max_months = int(os.environ.get("DATA_CHAM_ANH_MAX_MONTHS", str(DEFAULT_MAX_MONTHS)))
    root_folder = os.environ.get("DATA_CHAM_ANH_ROOT_FOLDER", DEFAULT_ROOT_FOLDER).strip("/")
    if not root_folder:
        raise ValueError("DATA_CHAM_ANH_ROOT_FOLDER must not be empty")

    start = parse_month(from_value, label="DATA_CHAM_ANH_FROM_MONTH")
    end = parse_month(to_value, label="DATA_CHAM_ANH_TO_MONTH")
    months = month_range(start, end, max_months=max_months)

    reports = core.enabled_reports()
    sharepoint = SemanticSharePointClient.from_env()
    drive_id = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
    if not drive_id:
        site_id = sharepoint.get_site_id()
        drive_id = sharepoint.get_drive_id(site_id)

    LOG.info(
        "Starting Data cham anh backfill from=%s to=%s months=%s root=%s",
        start.strftime("%Y-%m"),
        end.strftime("%Y-%m"),
        len(months),
        root_folder,
    )
    return publish_backfill(
        reports,
        sharepoint,
        drive_id,
        months,
        root_folder=root_folder,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run()
