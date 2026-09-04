from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

import main as core
from mobiwork import MobiWorkClient, ReportConfig
from monthly_master import SYNC_DATE_COLUMN, frames_from_records, master_filename, read_master, write_master
from sharepoint_semantic import SemanticSharePointClient


LOG = logging.getLogger("mobiwork_smoke")


def resolve_target_date(value: str | None = None) -> date:
    raw = (value or os.environ.get("SMOKE_TARGET_DATE", "")).strip()
    if raw:
        return date.fromisoformat(raw)
    return datetime.now(core.VN_TZ).date() - timedelta(days=1)


def _target_partition(frame: pd.DataFrame, target_date: date) -> pd.DataFrame:
    if SYNC_DATE_COLUMN not in frame.columns:
        raise ValueError(f"Missing {SYNC_DATE_COLUMN} in monthly master")
    partition = target_date.isoformat()
    return frame.loc[frame[SYNC_DATE_COLUMN].astype("string") == partition].reset_index(drop=True)


def _roundtrip_expected_frames(
    cfg: ReportConfig,
    target_date: date,
    records: list[dict[str, Any]],
) -> dict[str, pd.DataFrame]:
    frames = frames_from_records(records, cfg.export_mode, target_date)
    path = write_master(frames, f"__smoke_expected_{cfg.name}", target_date)
    try:
        return read_master(path.read_bytes(), cfg.export_mode)
    finally:
        path.unlink(missing_ok=True)


def compare_report_frames(
    actual: dict[str, pd.DataFrame],
    expected: dict[str, pd.DataFrame],
    target_date: date,
) -> dict[str, int]:
    compared_rows = 0
    for sheet_name, expected_frame in expected.items():
        if sheet_name not in actual:
            raise AssertionError(f"Missing sheet {sheet_name!r} in monthly master")

        actual_partition = _target_partition(actual[sheet_name], target_date)
        expected_partition = _target_partition(expected_frame, target_date)

        missing_columns = [col for col in expected_partition.columns if col not in actual_partition.columns]
        if missing_columns:
            raise AssertionError(
                f"Sheet {sheet_name}: missing columns in SharePoint master: {missing_columns}"
            )

        extra_columns = [col for col in actual_partition.columns if col not in expected_partition.columns]
        stale_extra = [col for col in extra_columns if not actual_partition[col].isna().all()]
        if stale_extra:
            raise AssertionError(
                f"Sheet {sheet_name}: target partition contains stale extra values in {stale_extra}"
            )

        actual_selected = actual_partition.loc[:, list(expected_partition.columns)].reset_index(drop=True)
        expected_selected = expected_partition.reset_index(drop=True)
        try:
            pd.testing.assert_frame_equal(
                actual_selected,
                expected_selected,
                check_dtype=False,
                check_exact=False,
                rtol=0,
                atol=0,
            )
        except AssertionError as exc:
            raise AssertionError(
                f"Sheet {sheet_name}: SharePoint partition does not match fresh MobiWork source: {exc}"
            ) from exc
        compared_rows += len(expected_selected)

    return {"compared_rows": compared_rows}


def evaluate_image_state(state: dict[str, Any] | None, target_date: date) -> dict[str, Any]:
    if not state:
        raise AssertionError("Data anh/_state.json is missing")

    completed_raw = str(state.get("last_completed_sync_date") or "").strip()
    if not completed_raw:
        raise AssertionError("Image state has no last_completed_sync_date")
    completed = date.fromisoformat(completed_raw)
    if completed < target_date:
        raise AssertionError(
            f"Image state is behind target date: completed={completed}, target={target_date}"
        )

    failed_count = int(state.get("failed_count") or 0)
    retry_from = str(state.get("retry_from_date") or "").strip() or None
    if failed_count > 0 or retry_from:
        raise AssertionError(
            f"Image state still has unresolved work: failed_count={failed_count}, retry_from_date={retry_from}"
        )

    return {
        "status": "success",
        "last_completed_sync_date": completed.isoformat(),
        "last_successful_sync_date": state.get("last_successful_sync_date"),
        "failed_count": failed_count,
        "retry_from_date": retry_from,
        "repairable": False,
    }


def _failure_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:4000]


def _mark_report_failure(
    item: dict[str, Any],
    *,
    stage: str,
    exc: Exception,
    repairable: bool,
) -> str:
    item.update(
        {
            "status": "failed",
            "failure_stage": stage,
            "repairable": repairable,
            "error": _failure_text(exc),
        }
    )
    return f"{item.get('report', '?')}: {item['error']}"


def _write_manifest(payload: dict[str, Any]) -> Path:
    output = Path("output")
    output.mkdir(parents=True, exist_ok=True)
    path = output / "production_smoke_manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def run_smoke(target_date: date | None = None) -> dict[str, Any]:
    target = target_date or resolve_target_date()
    reports = core.enabled_reports()
    manifest: dict[str, Any] = {
        "status": "running",
        "target_date": target.isoformat(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "reports": [],
    }

    sharepoint = SemanticSharePointClient.from_env()
    drive_id = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
    if not drive_id:
        site_id = sharepoint.get_site_id()
        drive_id = sharepoint.get_drive_id(site_id)
    mobiwork = MobiWorkClient.from_env()

    failures: list[str] = []
    for cfg in reports:
        item: dict[str, Any] = {
            "report": cfg.key,
            "status": "running",
            "repairable": False,
        }
        manifest["reports"].append(item)

        try:
            records = mobiwork.fetch_report(cfg, target)
            item["source_rows"] = len(records)
        except Exception as exc:
            failures.append(
                _mark_report_failure(
                    item,
                    stage="mobiwork_fetch",
                    exc=exc,
                    repairable=False,
                )
            )
            LOG.exception("Production smoke source fetch failed for report=%s", cfg.key)
            continue

        remote_folder = f"{cfg.folder}/{target:%Y}/{target:%m}"
        remote_path = f"{remote_folder}/{master_filename(cfg.name, target)}"
        item["remote_path"] = remote_path
        try:
            content = sharepoint.download_file_bytes(drive_id, remote_path)
        except Exception as exc:
            failures.append(
                _mark_report_failure(
                    item,
                    stage="sharepoint_read",
                    exc=exc,
                    repairable=False,
                )
            )
            LOG.exception("Production smoke SharePoint read failed for report=%s", cfg.key)
            continue

        if content is None:
            exc = AssertionError(f"SharePoint monthly master is missing: {remote_path}")
            failures.append(
                _mark_report_failure(
                    item,
                    stage="master_missing",
                    exc=exc,
                    repairable=True,
                )
            )
            LOG.error("Production smoke monthly master is missing for report=%s", cfg.key)
            continue

        try:
            actual = read_master(content, cfg.export_mode)
        except Exception as exc:
            failures.append(
                _mark_report_failure(
                    item,
                    stage="master_invalid",
                    exc=exc,
                    repairable=False,
                )
            )
            LOG.exception("Production smoke could not read monthly master for report=%s", cfg.key)
            continue

        try:
            expected = _roundtrip_expected_frames(cfg, target, records)
        except Exception as exc:
            failures.append(
                _mark_report_failure(
                    item,
                    stage="expected_transform",
                    exc=exc,
                    repairable=False,
                )
            )
            LOG.exception("Production smoke could not build expected frames for report=%s", cfg.key)
            continue

        try:
            comparison = compare_report_frames(actual, expected, target)
        except AssertionError as exc:
            failures.append(
                _mark_report_failure(
                    item,
                    stage="data_mismatch",
                    exc=exc,
                    repairable=True,
                )
            )
            LOG.exception("Production smoke data mismatch for report=%s", cfg.key)
            continue
        except Exception as exc:
            failures.append(
                _mark_report_failure(
                    item,
                    stage="comparison_error",
                    exc=exc,
                    repairable=False,
                )
            )
            LOG.exception("Production smoke comparison failed for report=%s", cfg.key)
            continue

        item.update(
            {
                "status": "success",
                "repairable": False,
                **comparison,
            }
        )

    try:
        image_state_payload = sharepoint.download_json(drive_id, "Data anh/_state.json")
    except Exception as exc:
        manifest["image_state"] = {
            "status": "failed",
            "failure_stage": "image_state_read",
            "repairable": False,
            "error": _failure_text(exc),
        }
        failures.append(f"images: {manifest['image_state']['error']}")
    else:
        try:
            manifest["image_state"] = evaluate_image_state(image_state_payload, target)
        except AssertionError as exc:
            manifest["image_state"] = {
                "status": "failed",
                "failure_stage": "image_state_consistency",
                "repairable": True,
                "error": _failure_text(exc),
            }
            failures.append(f"images: {manifest['image_state']['error']}")
        except Exception as exc:
            manifest["image_state"] = {
                "status": "failed",
                "failure_stage": "image_state_invalid",
                "repairable": False,
                "error": _failure_text(exc),
            }
            failures.append(f"images: {manifest['image_state']['error']}")

    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    manifest["successful_report_count"] = sum(
        item.get("status") == "success" for item in manifest["reports"]
    )
    manifest["failed_report_count"] = sum(
        item.get("status") == "failed" for item in manifest["reports"]
    )
    manifest["repairable_failure_count"] = sum(
        item.get("status") == "failed" and item.get("repairable") is True
        for item in manifest["reports"]
    ) + int(
        manifest.get("image_state", {}).get("status") == "failed"
        and manifest.get("image_state", {}).get("repairable") is True
    )
    manifest["status"] = "success" if not failures else "failed"
    if failures:
        manifest["error"] = "; ".join(failures)[:8000]
    _write_manifest(manifest)

    if failures:
        raise RuntimeError(
            f"Production smoke detected {len(failures)} consistency problem(s); "
            "see production_smoke_manifest.json"
        )
    return manifest


if __name__ == "__main__":
    from logging_config import configure

    configure()
    run_smoke()
