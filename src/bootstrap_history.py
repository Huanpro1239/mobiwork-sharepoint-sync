from __future__ import annotations

import calendar
import logging
import os
from datetime import date, datetime, timezone
from typing import Any

import main as core
import rebuild_month
import run_all_reports as runner
from bootstrap_gate import BOOTSTRAP_STATE_PATH


LOG = logging.getLogger("mobiwork_bootstrap")
DEFAULT_START_MONTH = "2026-06"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _parse_month(value: str, label: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM, for example 2026-06") from exc


def resolve_month_anchors(
    start_month: str | None = None,
    end_month: str | None = None,
) -> list[date]:
    """Return month anchors oldest-to-newest, ending at today for the current month."""
    today_vn = datetime.now(core.VN_TZ).date()
    current_month = today_vn.replace(day=1)
    start = _parse_month((start_month or DEFAULT_START_MONTH).strip(), "START_MONTH")
    end_raw = (end_month or "").strip()
    end = _parse_month(end_raw, "END_MONTH") if end_raw else current_month

    if start > current_month:
        raise ValueError(f"START_MONTH {start:%Y-%m} is in the future")
    if end > current_month:
        raise ValueError(f"END_MONTH {end:%Y-%m} is in the future")
    if end < start:
        raise ValueError("END_MONTH must be the same as or after START_MONTH")

    anchors: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor == current_month:
            anchor = today_vn
        else:
            last_day = calendar.monthrange(cursor.year, cursor.month)[1]
            anchor = cursor.replace(day=last_day)
        anchors.append(anchor)

        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    return anchors


def _bootstrap_state_payload(
    manifest: dict[str, Any],
    status: str,
    *,
    bootstrap_complete: bool,
) -> dict[str, Any]:
    return {
        "status": status,
        "bootstrap_complete": bootstrap_complete,
        "run_id": manifest.get("run_id"),
        "start_month": manifest.get("start_month"),
        "end_month": manifest.get("end_month"),
        "months_expected": manifest.get("months_expected", []),
        "months_completed": manifest.get("months_completed", []),
        "month_count_expected": manifest.get("month_count_expected", 0),
        "month_count_completed": manifest.get("month_count_completed", 0),
        "failed_month": manifest.get("failed_month"),
        "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_bootstrap_state(
    sharepoint: Any,
    drive_id: str | None,
    manifest: dict[str, Any],
    status: str,
    *,
    bootstrap_complete: bool,
) -> None:
    if not sharepoint or not drive_id:
        return
    payload = _bootstrap_state_payload(
        manifest,
        status,
        bootstrap_complete=bootstrap_complete,
    )
    sharepoint.upload_json(drive_id, BOOTSTRAP_STATE_PATH, payload)
    LOG.info(
        "Bootstrap readiness state updated status=%s complete=%s path=%s",
        status,
        bootstrap_complete,
        BOOTSTRAP_STATE_PATH,
    )


def run_bootstrap_set(
    reports: list[Any],
    anchors: list[date],
    mobiwork: Any,
    sharepoint: Any,
    drive_id: str | None,
    dry_run: bool,
    manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rebuild every month sequentially; stop before later months after the first failure."""
    results: list[dict[str, Any]] = []
    completed_months: list[str] = []

    for anchor in anchors:
        month = anchor.strftime("%Y-%m")
        LOG.info(
            "Bootstrap month started month=%s through=%s reports=%s",
            month,
            anchor.isoformat(),
            len(reports),
        )
        month_results = rebuild_month.run_rebuild_set(
            reports,
            anchor,
            mobiwork,
            sharepoint,
            drive_id,
            dry_run,
            manifest,
        )
        results.extend(month_results)

        failures = [item for item in month_results if item.get("status") != "success"]
        if failures:
            manifest["failed_month"] = month
            manifest["bootstrap_stop_reason"] = (
                "Stopped before later months because the current month did not fully rebuild"
            )
            LOG.error(
                "Bootstrap stopped month=%s failed_reports=%s",
                month,
                ",".join(str(item.get("report")) for item in failures),
            )
            break

        completed_months.append(month)
        manifest["months_completed"] = list(completed_months)
        manifest["month_count_completed"] = len(completed_months)
        if not dry_run:
            _write_bootstrap_state(
                sharepoint,
                drive_id,
                manifest,
                "running",
                bootstrap_complete=False,
            )
        LOG.info("Bootstrap month completed month=%s", month)

    return results, completed_months


def run() -> dict[str, Any]:
    dry_run = _env_bool("DRY_RUN", False)
    start_month = os.environ.get("START_MONTH", DEFAULT_START_MONTH)
    end_month = os.environ.get("END_MONTH", "")
    anchors = resolve_month_anchors(start_month, end_month)
    reports = core.enabled_reports()

    manifest = core._new_manifest("bootstrap_history", dry_run)
    manifest.update(
        {
            "reports": [cfg.key for cfg in reports],
            "start_month": anchors[0].strftime("%Y-%m"),
            "end_month": anchors[-1].strftime("%Y-%m"),
            "months_expected": [anchor.strftime("%Y-%m") for anchor in anchors],
            "months_completed": [],
            "month_count_expected": len(anchors),
            "month_count_completed": 0,
            "bootstrap_policy": "oldest_to_newest_full_month_rebuild",
            "schedule_handoff": "bootstrap_state_must_be_complete_before_production_writers_run",
            "storage_model": "single_monthly_master_per_report",
            "upsert_policy": "explicit_configured_business_keys",
            "completeness_gate": "all_reports_all_expected_days_per_month",
            "bootstrap_state_path": BOOTSTRAP_STATE_PATH,
        }
    )

    sharepoint = None
    drive_id: str | None = None

    try:
        mobiwork, sharepoint, drive_id = runner.build_clients(dry_run)
        if not dry_run:
            _write_bootstrap_state(
                sharepoint,
                drive_id,
                manifest,
                "running",
                bootstrap_complete=False,
            )

        results, completed_months = run_bootstrap_set(
            reports,
            anchors,
            mobiwork,
            sharepoint,
            drive_id,
            dry_run,
            manifest,
        )
        manifest["report_results"] = results
        manifest["months_completed"] = completed_months
        manifest["month_count_completed"] = len(completed_months)
        runner._finalize_manifest(manifest, results)

        fully_complete = len(completed_months) == len(anchors)
        manifest["bootstrap_complete"] = fully_complete and manifest["failed_report_count"] == 0
        if not manifest["bootstrap_complete"]:
            manifest["status"] = "failed"
            manifest["error"] = (
                manifest.get("error")
                or f"Bootstrap completed {len(completed_months)}/{len(anchors)} month(s)"
            )

        core._write_manifest(manifest)
        try:
            core._upload_manifest(manifest, sharepoint, drive_id)
        except Exception as exc:
            manifest["audit_upload_error"] = f"{type(exc).__name__}: {exc}"
            core._write_manifest(manifest)
            LOG.exception("Unable to upload bootstrap audit manifest")

        if not manifest["bootstrap_complete"]:
            if not dry_run:
                _write_bootstrap_state(
                    sharepoint,
                    drive_id,
                    manifest,
                    "failed",
                    bootstrap_complete=False,
                )
            raise RuntimeError(
                f"History bootstrap is incomplete: {len(completed_months)}/{len(anchors)} "
                "month(s) completed; see sync_manifest.json"
            )

        if not dry_run:
            _write_bootstrap_state(
                sharepoint,
                drive_id,
                manifest,
                "complete",
                bootstrap_complete=True,
            )
        return manifest
    except Exception:
        LOG.exception("bootstrap_history run failed")
        if manifest.get("status") == "running":
            manifest["status"] = "failed"
            manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
            manifest["bootstrap_complete"] = False
            core._write_manifest(manifest)
            try:
                core._upload_manifest(manifest, sharepoint, drive_id)
            except Exception:
                LOG.exception("Unable to upload bootstrap failure manifest")
        if not dry_run and sharepoint and drive_id:
            try:
                _write_bootstrap_state(
                    sharepoint,
                    drive_id,
                    manifest,
                    "failed",
                    bootstrap_complete=False,
                )
            except Exception:
                LOG.exception("Unable to update failed bootstrap readiness state")
        raise


if __name__ == "__main__":
    from logging_config import configure

    configure()
    run()
