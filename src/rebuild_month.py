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
    """Resolve the last business date that should be fetched for a YYYY-MM month."""
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


def _prepare_report_month(cfg: Any, anchor: date, mobiwork: Any) -> dict[str, Any]:
    """Fetch every expected daily partition and build a local monthly workbook."""
    target_dates = month_dates_through(anchor)
    partitions: list[tuple[date, list[dict[str, Any]]]] = []
    daily_source_rows: dict[str, int] = {}

    LOG.info(
        "Full month source preparation started report=%s month=%s days=%s",
        cfg.key,
        anchor.strftime("%Y-%m"),
        len(target_dates),
    )

    for target_date in target_dates:
        records = mobiwork.fetch_report(cfg, target_date)
        partitions.append((target_date, records))
        daily_source_rows[target_date.isoformat()] = len(records)

    frames = build_month_from_partitions(
        partitions,
        cfg.export_mode,
        upsert_keys=cfg.upsert_keys,
    )
    path = write_master(frames, cfg.name, anchor)
    remote_folder = f"{cfg.folder}/{anchor:%Y}/{anchor:%m}"

    return {
        "cfg": cfg,
        "anchor": anchor,
        "path": path,
        "remote_folder": remote_folder,
        "canonical_name": master_filename(cfg.name, anchor),
        "source_rows": sum(daily_source_rows.values()),
        "master_rows": master_row_count(frames, cfg.export_mode),
        "target_dates": target_dates,
        "daily_source_rows": daily_source_rows,
    }


def _publish_prepared_report(
    bundle: dict[str, Any],
    sharepoint: Any,
    drive_id: str | None,
    dry_run: bool,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Publish one already-prepared workbook after the global source gate passes."""
    cfg = bundle["cfg"]
    anchor = bundle["anchor"]
    path = bundle["path"]
    target_dates = bundle["target_dates"]
    uploaded: dict[str, Any] | None = None

    if not dry_run:
        if not sharepoint or not drive_id:
            raise RuntimeError("SharePoint client is unavailable in production mode")
        uploaded = sharepoint.upload_file(drive_id, path, bundle["remote_folder"])
        runner._cleanup_legacy_files(
            sharepoint,
            drive_id,
            bundle["remote_folder"],
            cfg.name,
            bundle["canonical_name"],
        )

    runner._record_monthly_export(
        manifest,
        cfg,
        path,
        bundle["source_rows"],
        bundle["master_rows"],
        bundle["remote_folder"],
        uploaded,
        target_dates=target_dates,
    )
    export = manifest["files"][-1]
    export["rebuild_mode"] = "full_month_from_api"
    export["days_expected"] = len(target_dates)
    export["days_fetched"] = len(bundle["daily_source_rows"])
    export["all_days_fetched"] = len(bundle["daily_source_rows"]) == len(target_dates)
    export["daily_source_rows"] = bundle["daily_source_rows"]
    export["source_gate_passed"] = True

    result = _result_entry(cfg, anchor)
    result.update(
        {
            "status": "success",
            "filename": path.name,
            "local_size_bytes": path.stat().st_size,
            "remote_folder": bundle["remote_folder"],
            "source_rows": bundle["source_rows"],
            "master_rows": bundle["master_rows"],
            "month_rebuilt": True,
            "rebuild_days": len(target_dates),
            "days_expected": len(target_dates),
            "days_fetched": len(bundle["daily_source_rows"]),
            "all_days_fetched": True,
            "source_gate_passed": True,
            "verification_mode": uploaded.get("verification_mode") if uploaded else "dry-run",
            "semantic_match": uploaded.get("semantic_match") if uploaded else None,
            "upload_skipped": bool(uploaded.get("upload_skipped", False)) if uploaded else False,
            "web_url": uploaded.get("webUrl") if uploaded else None,
        }
    )
    return result


def run_rebuild_set(
    reports: list[Any],
    anchor: date,
    mobiwork: Any,
    sharepoint: Any,
    drive_id: str | None,
    dry_run: bool,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """Prepare every source first; publish nothing unless all report sources pass."""
    prepared: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}

    for cfg in reports:
        try:
            prepared[cfg.key] = _prepare_report_month(cfg, anchor, mobiwork)
        except Exception as exc:
            LOG.exception(
                "Full month source preparation failed report=%s month=%s",
                cfg.key,
                anchor.strftime("%Y-%m"),
            )
            failed = _result_entry(cfg, anchor)
            failed.update(
                {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "month_rebuilt": False,
                    "source_gate_passed": False,
                }
            )
            results[cfg.key] = failed

    if results:
        failed_keys = ", ".join(sorted(results))
        for cfg in reports:
            if cfg.key in results:
                continue
            bundle = prepared[cfg.key]
            blocked = _result_entry(cfg, anchor)
            blocked.update(
                {
                    "status": "failed",
                    "error": (
                        "Publish blocked by source completeness gate; failed report(s): "
                        f"{failed_keys}"
                    ),
                    "source_rows": bundle["source_rows"],
                    "master_rows": bundle["master_rows"],
                    "month_rebuilt": False,
                    "days_expected": len(bundle["target_dates"]),
                    "days_fetched": len(bundle["daily_source_rows"]),
                    "all_days_fetched": True,
                    "source_gate_passed": False,
                }
            )
            results[cfg.key] = blocked
        return [results[cfg.key] for cfg in reports]

    publish_failed = False
    publish_error = ""
    for cfg in reports:
        bundle = prepared[cfg.key]
        if publish_failed:
            blocked = _result_entry(cfg, anchor)
            blocked.update(
                {
                    "status": "failed",
                    "error": f"Publish blocked after prior SharePoint failure: {publish_error}",
                    "source_rows": bundle["source_rows"],
                    "master_rows": bundle["master_rows"],
                    "month_rebuilt": False,
                    "source_gate_passed": True,
                }
            )
            results[cfg.key] = blocked
            continue

        try:
            results[cfg.key] = _publish_prepared_report(
                bundle,
                sharepoint,
                drive_id,
                dry_run,
                manifest,
            )
        except Exception as exc:
            LOG.exception(
                "Full month publish failed report=%s month=%s; later reports blocked",
                cfg.key,
                anchor.strftime("%Y-%m"),
            )
            publish_failed = True
            publish_error = f"{type(exc).__name__}: {exc}"
            failed = _result_entry(cfg, anchor)
            failed.update(
                {
                    "status": "failed",
                    "error": publish_error,
                    "source_rows": bundle["source_rows"],
                    "master_rows": bundle["master_rows"],
                    "month_rebuilt": False,
                    "source_gate_passed": True,
                }
            )
            results[cfg.key] = failed

    return [results[cfg.key] for cfg in reports]


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
    manifest["completeness_gate"] = "all_reports_all_expected_days_before_first_write"
    manifest["publish_policy"] = "stop_after_first_sharepoint_failure"
    manifest["upsert_policy"] = "explicit_configured_business_keys"

    sharepoint = None
    drive_id: str | None = None

    try:
        mobiwork, sharepoint, drive_id = runner.build_clients(dry_run)
        results = run_rebuild_set(
            reports,
            anchor,
            mobiwork,
            sharepoint,
            drive_id,
            dry_run,
            manifest,
        )
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
        LOG.exception("rebuild_month failed")
        if manifest.get("status") == "running":
            manifest["status"] = "failed"
            manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
            core._write_manifest(manifest)
        raise


if __name__ == "__main__":
    from logging_config import configure

    configure()
    run()
