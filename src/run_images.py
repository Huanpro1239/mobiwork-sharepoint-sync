from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import main as core
from image_sync import ImageSyncConfig, run_image_sync
from mobiwork import MobiWorkClient, ReportConfig
from sharepoint_semantic import SemanticSharePointClient


LOG = logging.getLogger("mobiwork_sync")


class DailyRangeMobiWorkClient(MobiWorkClient):
    """Fetch image-source ranges as daily partitions.

    The normal report pipeline already queries MobiWork one day at a time. Image sync
    can backfill almost two calendar months on its first run, so sending that whole
    range in one VisitData request is unnecessarily large and can be rejected or time
    out. Split it into deterministic daily calls, while later incremental runs still
    require only the one-day overlap plus today.
    """

    def fetch_report_range(
        self,
        cfg: ReportConfig,
        from_date: date,
        to_date: date,
    ) -> list[dict]:
        if to_date < from_date:
            raise ValueError("to_date must be on or after from_date")

        records: list[dict] = []
        current = from_date
        while current <= to_date:
            LOG.info("Image metadata fetch date: %s", current.isoformat())
            records.extend(super().fetch_report_range(cfg, current, current))
            current += timedelta(days=1)
        return records


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _write_manifest(payload: dict) -> Path:
    output = Path("output")
    output.mkdir(parents=True, exist_ok=True)
    path = output / "image_sync_manifest.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def run() -> dict:
    dry_run = _env_bool("DRY_RUN", False)
    reports = core.enabled_reports()
    today = datetime.now(core.VN_TZ).date()
    config = ImageSyncConfig.from_env()

    manifest = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "today_vn": today.isoformat(),
        "timezone": "Asia/Ho_Chi_Minh",
        "dry_run": dry_run,
        "config": {
            "source_report": config.source_report_key,
            "root_folder": config.root_folder,
            "url_field": config.url_field,
            "date_field": config.date_field,
            "employee_field": config.employee_field,
            "customer_field": config.customer_field,
            "sequence_field": config.sequence_field,
            "require_ghi_ton": config.require_ghi_ton,
            "metadata_fetch_mode": "daily_partitions",
        },
    }

    mobiwork = DailyRangeMobiWorkClient.from_env()
    sharepoint = None
    drive_id = None

    if not dry_run:
        sharepoint = SemanticSharePointClient.from_env()
        drive_id = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
        if not drive_id:
            site_id = sharepoint.get_site_id()
            drive_id = sharepoint.get_drive_id(site_id)

    try:
        result = run_image_sync(
            reports=reports,
            mobiwork=mobiwork,
            sharepoint=sharepoint,
            drive_id=drive_id,
            dry_run=dry_run,
            today=today,
            cfg=config,
        )
        manifest["image_sync"] = result
        manifest["status"] = result.get("status", "unknown")
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_manifest(manifest)

        if result.get("status") in {"failed", "partial_failure"}:
            raise RuntimeError(
                f"Image sync finished with status={result.get('status')}; "
                "see output/image_sync_manifest.json"
            )
        return manifest
    except Exception as exc:
        manifest.setdefault("status", "failed")
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_manifest(manifest)
        raise


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run()
