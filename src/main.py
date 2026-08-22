from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mobiwork import ReportConfig
from sharepoint import SharePointClient


LOG = logging.getLogger("mobiwork_sync")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


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
