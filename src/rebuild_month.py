from __future__ import annotations

import calendar
import logging
import os
from datetime import date, datetime, timezone
from typing import Any

import main as core
import run_all_reports as runner
from monthly_master import (
    build_month_from_partitions,
    master_filename,
    master_row_count,
    month_dates_through,
    write_master,
)


LOG = logging.getLogger("mobiwork_sync")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def resolve_anchor(target_month: str | None = None) -> date:
    """Resolve the last business date that should be fetched for a YYYY-MM month.

    - blank/current month -> today in Vietnam
    - past month -> calendar month end
    - future month -> rejected
    """
    today_vn = datetime.now(core.VN_TZ).date()
    value = (target_month or "").strip()
    if not value:
        return today_vn

    try:
        month_start = datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise ValueError("TARGET_MONTH must use YYYY-MM, for example 2026-09") from exc

    current_month = today_vn.replace(day=1)
    if month_start > current_month:
        raise ValueError(f"TARGET_MONTH {value} is in the future")
    if month_start == current_month:
        return today_vn

    last_day = calendar.monthrange(month_start.year, month_start.month)[1]
    return month_start.replace(day=last_day)


def _result_entry(cfg: Any, anchor: date) -> dict[str, Any]:
    return {
        "report": cfg.key,
        "report_name": cfg.name,
        "target_date": anchor.isoformat(),
        "month_master": f"{anchor:%Y-%m}",
        "status": "running",
    }


def _rebuild_report_month(
    cfg: Any,
    anchor: date,
    mobiwork: Any,
    sharepoint: Any,
    drive_id: str | None,
    dry_run: bool,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild one report/month only from MobiWork API partitions.

    The existing SharePoint workbook is deliberately not read. Every expected day
    must fetch successfully before a replacement monthly master can be published.
    """
    target_dates = month_dates_through(anchor)
    partitions: list[tuple[date, list[dict[str, Any]]]] = []
    daily_source_rows: dict[str, int] = {}

    LOG.info(
        "Full month rebuild started report=%s month=%s days=%s",
        cfg.key,
        anchor.strftime("%Y-%m"),
        len(target_dates),
    )

    for target_date in target_dates:
        records = mobiwork.fetch_report(cfg, target_date)
        partitions.append((target_date, records))
        daily_source_rows[target_date.isoformat()] = len(records)

    frames = build_month_from_partitions(partitions, cfg.export_mode)
    path = write_master(frames, cfg.name, anchor)
    stored_rows = master_row_count(frames, cfg.export_mode)
    source_rows = sum(daily_source_rows.values())
    remote_folder = f"{cfg.folder}/{anchor:%Y}/{anchor:%m}"
    canonical_name = master_filename(cfg.name, anchor)

    uploaded: dict[str, Any] | None = None
    if not dry_run:
        if not sharepoint or not drive_id:
            raise RuntimeError("SharePoint client is unavailable in production mode")
        uploaded = sharepoint.upload_file(drive_id, path, remote_folder)
        runner._cleanup_legacy_files(
            sharepoint,
            drive_id,
            remote_folder,
            cfg.name,
            canonical_name,
        )

    runner._record_monthly_export(
        manifest,
        cfg,
        path,
        source_rows,
        stored_rows,
        remote_folder,
        uploaded,
        target_dates=target_dates,
    )
    export = manifest["files"][-1]
    export["rebuild_mode"] = "full_month_from_api"
    export["days_expected"] = len(target_dates)
    export["days_fetched"] = len(daily_source_rows)
    export["all_days_fetched"] = len(daily_source_rows) == len(target_dates)
    export["daily_source_rows"] = daily_source_rows

    result = _result_entry(cfg, anchor)
    result.update(
        {
            "status": "success",
            "filename": path.name,
            "local_size_bytes": path.stat().st_size,
            "remote_folder": remote_folder,
            "source_rows": source_rows,
            "master_rows": stored_rows,
            "month_rebuilt": True,
            "rebuild_days": len(target_dates),
            "days_expected": len(target_dates),
            "days_fetched": len(daily_source_rows),
            "all_days_fetched": True,
            "verification_mode": uploaded.get("verification_mode") if uploaded else "dry-run",
            "semantic_match": uploaded.get("semantic_match") if uploaded else None,
            "upload_skipped": bool(uploaded.get("upload_skipped", False)) if uploaded else False,
            "web_url": uploaded.get("webUrl") if uploaded else None,
        }
    )
    return result


def run() -> dict[str, Any]:
    dry_run = _env_bool("DRY_RUN", False)
    anchor = resolve_anchor(os.environ.get("TARGET_MONTH"))
    reports = core.enabled_reports()
    manifest = core._new_manifest("rebuild_month", dry_run)
    manifest["reports"] = [cfg.key for cfg in reports]
    manifest["target_month"] = f"{anchor:%Y-%m}"
    manifest["rebuild_through"] = anchor.isoformat()
    manifest["storage_model"] = "single_monthly_master_per_report"
    manifest["rebuild_policy"] = "full_month_from_api_no_existing_master_read"
    manifest["completeness_gate"] = "all_expected_daily_fetches_must_succeed"

    sharepoint = None
    drive_id: str | None = None
    results: list[dict[str, Any]] = []

    try:
        mobiwork, sharepoint, drive_id = runner.build_clients(dry_run)

        for cfg in reports:
            try:
                results.append(
                    _rebuild_report_month(
                        cfg,
                        anchor,
                        mobiwork,
                        sharepoint,
                        drive_id,
                        dry_run,
                        manifest,
                    )
                )
            except Exception as exc:
                LOG.exception(
                    "Full month rebuild failed; existing SharePoint master left untouched: "
                    "report=%s month=%s",
                    cfg.key,
                    anchor.strftime("%Y-%m"),
                )
                failed = _result_entry(cfg, anchor)
                failed["status"] = "failed"
                failed["error"] = f"{type(exc).__name__}: {exc}"
                failed["month_rebuilt"] = False
                results.append(failed)

        manifest["report_results"] = results
        runner._finalize_manifest(manifest, results)
        core._write_manifest(manifest)
        try:
            core._upload_manifest(manifest, sharepoint, drive_id)
        except Exception as exc:
            manifest["audit_upload_error"] = f"{type(exc).__name__}: {exc}"
            core._write_manifest(manifest)
            LOG.exception("Unable to upload rebuild audit manifest")

        if manifest["failed_report_count"]:
            raise RuntimeError(
                f"{manifest['failed_report_count']} report rebuild(s) failed after "
                f"{manifest['successful_report_count']} succeeded; see sync_manifest.json"
            )
        return manifest
    except Exception:
        if manifest.get("status") == "running":
            manifest["status"] = "failed"
            manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
            core._write_manifest(manifest)
        raise


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run()
