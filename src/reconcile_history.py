from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import bootstrap_history
import main as core
import rebuild_month
import run_all_reports as runner


LOG = logging.getLogger("mobiwork_history_reconcile")
DEFAULT_START_MONTH = bootstrap_history.DEFAULT_START_MONTH


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def previous_month_label() -> str:
    today_vn = datetime.now(core.VN_TZ).date()
    previous_month_end = today_vn.replace(day=1) - timedelta(days=1)
    return previous_month_end.strftime("%Y-%m")


def resolve_completed_history_anchors(
    start_month: str | None = None,
    end_month: str | None = None,
) -> list[date]:
    """Resolve completed calendar months oldest-to-newest.

    The scheduled path intentionally excludes the current open month because weekly
    recovery already rebuilds it. This job exists to catch late/back-dated changes in
    older source records that no ordinary lookback window can see.
    """
    start = (start_month or DEFAULT_START_MONTH).strip()
    end = (end_month or "").strip() or previous_month_label()
    return bootstrap_history.resolve_month_anchors(start, end)


def run_reconcile_set(
    reports: list[Any],
    anchors: list[date],
    mobiwork: Any,
    sharepoint: Any,
    drive_id: str | None,
    dry_run: bool,
    manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rebuild completed months sequentially and stop after the first incomplete month."""
    results: list[dict[str, Any]] = []
    completed_months: list[str] = []

    for anchor in anchors:
        month = anchor.strftime("%Y-%m")
        LOG.info("Historical reconciliation started month=%s", month)
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
            manifest["history_reconcile_stop_reason"] = (
                "Stopped before later months because this month did not fully reconcile"
            )
            LOG.error(
                "Historical reconciliation stopped month=%s failed_reports=%s",
                month,
                ",".join(str(item.get("report")) for item in failures),
            )
            break

        completed_months.append(month)
        LOG.info("Historical reconciliation completed month=%s", month)

    return results, completed_months


def run() -> dict[str, Any]:
    dry_run = _env_bool("DRY_RUN", False)
    start_month = os.environ.get("HISTORY_START_MONTH", DEFAULT_START_MONTH)
    end_month = os.environ.get("HISTORY_END_MONTH", "")
    anchors = resolve_completed_history_anchors(start_month, end_month)
    reports = core.enabled_reports()

    manifest = core._new_manifest("historical_reconcile", dry_run)
    manifest.update(
        {
            "reports": [cfg.key for cfg in reports],
            "start_month": anchors[0].strftime("%Y-%m"),
            "end_month": anchors[-1].strftime("%Y-%m"),
            "months_expected": [anchor.strftime("%Y-%m") for anchor in anchors],
            "months_completed": [],
            "month_count_expected": len(anchors),
            "month_count_completed": 0,
            "storage_model": "single_monthly_master_per_report",
            "reconcile_policy": "all_completed_history_full_month_from_api",
            "completeness_gate": "all_reports_all_expected_days_per_month",
            "bootstrap_state_policy": "readiness_required_but_state_not_modified",
            "upsert_policy": "explicit_configured_business_keys",
        }
    )

    sharepoint = None
    drive_id: str | None = None

    try:
        mobiwork, sharepoint, drive_id = runner.build_clients(dry_run)
        results, completed_months = run_reconcile_set(
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
        manifest["history_reconcile_complete"] = (
            fully_complete and manifest["failed_report_count"] == 0
        )
        if not manifest["history_reconcile_complete"]:
            manifest["status"] = "failed"
            manifest["error"] = (
                manifest.get("error")
                or f"Historical reconciliation completed {len(completed_months)}/"
                f"{len(anchors)} month(s)"
            )

        core._write_manifest(manifest)
        try:
            core._upload_manifest(manifest, sharepoint, drive_id)
        except Exception as exc:
            manifest["audit_upload_error"] = f"{type(exc).__name__}: {exc}"
            core._write_manifest(manifest)
            LOG.exception("Unable to upload historical reconciliation audit manifest")

        if not manifest["history_reconcile_complete"]:
            raise RuntimeError(
                f"Historical reconciliation is incomplete: {len(completed_months)}/"
                f"{len(anchors)} month(s) completed; see sync_manifest.json"
            )
        return manifest
    except Exception:
        if manifest.get("status") == "running":
            manifest["status"] = "failed"
            manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
            manifest["history_reconcile_complete"] = False
            core._write_manifest(manifest)
            try:
                core._upload_manifest(manifest, sharepoint, drive_id)
            except Exception:
                LOG.exception("Unable to upload historical reconciliation failure manifest")
        raise


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run()
