"""Coordinate existing report commands with one catalogue snapshot per run."""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

import main as core
import promotion_bonus
import promotion_detail
import run_all_reports
import run_data_cham_anh
from promotion_catalogue import load_config
from promotion_months import parse_requested_months
from promotion_workbook import LAYOUT
from report_context import CatalogueCache
from report_runtime import env_bool, temporary_environment, write_manifest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path("output/pipeline_manifest.json")
SCOPES = {
    "all_reports": ("reports", "photos", "promotion_bonus", "promotion_detail"),
    "promotion_history": ("promotion_bonus", "promotion_detail"),
    "promotion_bonus_only": ("promotion_bonus",),
}
SUCCESS_STATES = {"success", "skipped", "needs_mapping", "published_with_issues"}


def check() -> dict[str, Any]:
    """Validate production configuration offline, without credentials or writes."""
    reports = [report for report in core.load_reports(ROOT / "config/reports.json") if report.enabled]
    if not reports or len({report.key for report in reports}) != len(reports):
        raise ValueError("Enabled report keys must be nonempty and unique")
    if "bill" not in {report.key for report in reports}:
        raise ValueError("Promotion reports require an enabled bill report")
    load_config()
    promotion_detail.load_config()
    layout = json.loads(LAYOUT.read_text(encoding="utf-8"))
    headers = [label.replace("\n", " ") for label in layout["headers"]]
    if len(headers) == 26:
        headers[24] += " Tặng"  # The printed template repeats "Tên Sản phẩm".
    if headers != promotion_detail.COLUMNS or len(layout["widths"]) != 26:
        raise ValueError("Promotion exports must share the 26-column customer template")
    return {"reports": [report.key for report in reports], "scopes": list(SCOPES),
            "template_columns": len(headers)}


def _result_summary(result: Any) -> dict[str, Any]:
    # The pipeline manifest contains operational metadata only. Row-level data
    # stays in its existing report and is never copied into Actions artifacts.
    if isinstance(result, list):
        if any(not isinstance(item, dict) or item.get("status") not in SUCCESS_STATES
               for item in result):
            raise RuntimeError("Photo report returned an unsuccessful result")
        return {"status": "success" if result else "skipped", "file_count": len(result)}
    if not isinstance(result, dict) or result.get("status") not in SUCCESS_STATES:
        status = result.get("status") if isinstance(result, dict) else type(result).__name__
        raise RuntimeError(f"Report returned an unsuccessful result: {status}")
    if result.get("publish_failures") or result.get("failed_report_count", 0):
        raise RuntimeError("Report returned incomplete publication or failed source executions")
    summary = {key: result[key] for key in ("status", "quality_status", "program_count",
                                          "customer_row_count", "blocking_issues", "template_issues",
                                          "successful_report_count", "failed_report_count") if key in result}
    summary["needs_review"] = (result.get("quality_status") == "needs_review"
                               or result["status"] in {"needs_mapping", "published_with_issues"}
                               or any(item.get("quality_status") == "needs_review"
                                      or item.get("error")
                                      for item in result.get("months", {}).values()))
    return summary


def _scope_settings(scope: str) -> dict[str, str]:
    if scope != "promotion_history":
        return {}
    today = datetime.now(core.VN_TZ).date()

    def normalize(value: str) -> str:
        if not value.strip() or value.strip().casefold() == "all_existing":
            return "all_existing"
        return ",".join(month.strftime("%Y-%m") for month in parse_requested_months(value, today))

    bonus = os.environ.get("PROMOTION_BONUS_MONTHS", "").strip()
    detail = os.environ.get("PROMOTION_DETAIL_MONTHS", "").strip()
    if bonus and detail and normalize(bonus) != normalize(detail):
        raise ValueError("Historical bonus and CTKM must select the same months")
    selected = normalize(bonus or detail)
    return {"PROMOTION_BONUS_MONTHS": selected, "PROMOTION_DETAIL_MONTHS": selected,
            "PROMOTION_DETAIL_SCOPE": "all_existing"}


def run(scope: str = "all_reports") -> dict[str, Any]:
    if scope not in SCOPES:
        raise ValueError(f"Unknown report scope: {scope}")
    settings = _scope_settings(scope)
    cache = CatalogueCache()
    manifest: dict[str, Any] = {
        "status": "running", "scope": scope, "dry_run": env_bool("DRY_RUN"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stages": [{"name": name, "status": "pending"} for name in SCOPES[scope]],
    }
    runners = {
        "reports": run_all_reports.run,
        "photos": run_data_cham_anh.run,
        "promotion_bonus": lambda: promotion_bonus.run(cache),
        "promotion_detail": lambda: promotion_detail.run(cache),
    }
    write_manifest(MANIFEST_PATH, manifest)
    try:
        with temporary_environment(settings):
            for stage in manifest["stages"]:
                start = monotonic()
                stage["status"] = "running"
                write_manifest(MANIFEST_PATH, manifest)
                logging.info("Pipeline stage: %s", stage["name"])
                try:
                    stage.update(_result_summary(runners[stage["name"]]()))
                except Exception as exc:
                    stage.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                    raise
                finally:
                    stage["duration_seconds"] = round(monotonic() - start, 3)
                    manifest["catalogue_loads"] = dict(cache.loads)
                    write_manifest(MANIFEST_PATH, manifest)
        manifest["status"] = "success"
        manifest["needs_review"] = any(stage.get("needs_review", False) for stage in manifest["stages"])
        return manifest
    except Exception as exc:
        manifest.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        for stage in manifest["stages"]:
            if stage["status"] == "pending":
                stage.update(status="skipped", reason="Earlier stage failed")
        raise
    finally:
        manifest["needs_review"] = any(stage.get("needs_review", False) for stage in manifest["stages"])
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_manifest(MANIFEST_PATH, manifest)


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=SCOPES, default=os.environ.get("REPORT_SCOPE", "all_reports"))
    parser.add_argument("--check", action="store_true", help="Offline configuration and import check")
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s | %(levelname)s | %(message)s")
    if args.check:
        print(check())
    else:
        run(args.scope)


if __name__ == "__main__":
    cli()
