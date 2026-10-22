"""Coordinate the MobiWork report sync and the Data cham anh export in one run."""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

import main as core
import run_all_reports
import run_data_cham_anh
from report_runtime import env_bool, write_manifest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path("output/pipeline_manifest.json")
SCOPES = {"all_reports": ("reports", "photos")}
SUCCESS_STATES = {"success", "skipped"}


def check() -> dict[str, Any]:
    """Validate production configuration offline, without credentials or writes."""
    reports = [report for report in core.load_reports(ROOT / "config/reports.json") if report.enabled]
    if not reports or len({report.key for report in reports}) != len(reports):
        raise ValueError("Enabled report keys must be nonempty and unique")
    return {"reports": [report.key for report in reports], "scopes": list(SCOPES)}


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
    return {key: result[key] for key in ("status", "successful_report_count", "failed_report_count")
            if key in result}


def run(scope: str = "all_reports") -> dict[str, Any]:
    if scope not in SCOPES:
        raise ValueError(f"Unknown report scope: {scope}")
    manifest: dict[str, Any] = {
        "status": "running", "scope": scope, "dry_run": env_bool("DRY_RUN"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stages": [{"name": name, "status": "pending"} for name in SCOPES[scope]],
    }
    runners = {"reports": run_all_reports.run, "photos": run_data_cham_anh.run}
    write_manifest(MANIFEST_PATH, manifest)
    try:
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
                write_manifest(MANIFEST_PATH, manifest)
        manifest["status"] = "success"
        return manifest
    except Exception as exc:
        manifest.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        for stage in manifest["stages"]:
            if stage["status"] == "pending":
                stage.update(status="skipped", reason="Earlier stage failed")
        raise
    finally:
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
