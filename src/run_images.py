from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import main as core
from image_storage import ImageSharePointClient
from image_sync import ImageSyncConfig, run_image_sync
from mobiwork import MobiWorkClient
from sharepoint_image_source import SharePointMonthlyImageSource


LOG = logging.getLogger("mobiwork_sync")


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
    fail_on_partial = _env_bool("IMAGE_FAIL_ON_PARTIAL", False)
    reports = core.enabled_reports()
    today = datetime.now(core.VN_TZ).date()
    config = ImageSyncConfig.from_env()

    manifest = {
        "schema_version": 2,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "today_vn": today.isoformat(),
        "timezone": "Asia/Ho_Chi_Minh",
        "dry_run": dry_run,
        "source_mode": "sharepoint_monthly_master",
        "config": {
            "source_report": config.source_report_key,
            "root_folder": config.root_folder,
            "url_field": config.url_field,
            "date_field": config.date_field,
            "employee_field": config.employee_field,
            "customer_field": config.customer_field,
            "sequence_field": config.sequence_field,
            "require_ghi_ton": config.require_ghi_ton,
            "fail_on_partial": fail_on_partial,
            "request_timeout_seconds": config.request_timeout,
            "max_download_retries": config.max_download_retries,
            "max_image_bytes": config.max_image_bytes,
            "allowed_hosts": list(config.allowed_hosts),
            "force_from_date": (
                config.force_from_date.isoformat() if config.force_from_date else None
            ),
        },
    }

    # MobiWork credentials are used only for image-byte downloads. Metadata is read
    # from the monthly visit workbook already persisted to SharePoint.
    mobiwork = MobiWorkClient.from_env()
    storage = ImageSharePointClient.from_env()
    drive_id = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
    if not drive_id:
        site_id = storage.get_site_id()
        drive_id = storage.get_drive_id(site_id)

    source = SharePointMonthlyImageSource(
        mobiwork=mobiwork,
        sharepoint=storage,
        drive_id=drive_id,
    )

    try:
        result = run_image_sync(
            reports=reports,
            source=source,
            storage=storage,
            drive_id=drive_id,
            dry_run=dry_run,
            today=today,
            cfg=config,
        )
        result["source_mode"] = "sharepoint_monthly_master"
        result["source_files"] = list(source.source_files)
        manifest["image_sync"] = result
        manifest["status"] = result.get("status", "unknown")
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_manifest(manifest)

        if result.get("status") == "failed" or (
            result.get("status") == "partial_failure" and fail_on_partial
        ):
            raise RuntimeError(
                f"Image sync finished with status={result.get('status')}; "
                "see output/image_sync_manifest.json"
            )
        return manifest
    except Exception as exc:
        manifest.setdefault("status", "failed")
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["source_files"] = list(source.source_files)
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_manifest(manifest)
        raise


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run()
