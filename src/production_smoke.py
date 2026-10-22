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
from monthly_master import SYNC_DATE_COLUMN, _normalize_key_value, frames_from_records, master_filename, read_master, write_master
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


# Order documents are approved and edited in MobiWork for hours after the sync
# writes them (status, approver, totals, discounts, even ngay_dat and lines).
# A document may differ from the fresh source only when MobiWork shows a newer
# revision stamp for it, and its identity columns must still match.  Every
# other document, and the document/line sets themselves, must match exactly.
ORDER_REVISION_COLUMNS = ("ngay_sua", "ngay_duyet")
ORDER_IMMUTABLE_COLUMNS = ("ma_phieu", "ngay_tao", "ma_kh", "ma_nv_dat")


def _aligned_partitions(
    actual: dict[str, pd.DataFrame],
    expected: dict[str, pd.DataFrame],
    sheet_name: str,
    target_date: date,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if sheet_name not in actual:
        raise AssertionError(f"Missing sheet {sheet_name!r} in monthly master")

    actual_partition = _target_partition(actual[sheet_name], target_date)
    expected_partition = _target_partition(expected[sheet_name], target_date)

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
    return actual_selected, expected_partition.reset_index(drop=True)


def _assert_partition_equal(
    actual_selected: pd.DataFrame,
    expected_selected: pd.DataFrame,
    label: str,
) -> None:
    actual_selected = actual_selected.reset_index(drop=True)
    expected_selected = expected_selected.reset_index(drop=True)
    # An all-empty date column in a row subset reads back as NaN, not NaT,
    # when no row of that workbook had a value; treat it as an empty date.
    for frame, other in ((actual_selected, expected_selected), (expected_selected, actual_selected)):
        for column in frame.columns:
            if (
                pd.api.types.is_datetime64_any_dtype(other[column])
                and not pd.api.types.is_datetime64_any_dtype(frame[column])
                and frame[column].isna().all()
            ):
                frame[column] = pd.Series(pd.NaT, index=frame.index, dtype=other[column].dtype)
    try:
        pd.testing.assert_frame_equal(
            actual_selected,
            expected_selected,
            check_dtype=False,
            check_exact=False,
            rtol=0,
            atol=0,
        )
    except AssertionError:
        # Pandas includes complete column contents in assertion messages.
        # These can contain customer telephone numbers, names and addresses:
        # never write them to public CI logs, GitHub summaries or manifests.
        differing_columns = []
        for column in expected_selected.columns:
            try:
                pd.testing.assert_series_equal(
                    actual_selected[column],
                    expected_selected[column],
                    check_dtype=False,
                    check_exact=False,
                    rtol=0,
                    atol=0,
                )
            except AssertionError:
                differing_columns.append(str(column))
        raise AssertionError(
            f"Sheet {label}: SharePoint partition does not match fresh MobiWork source; "
            f"actual_rows={len(actual_selected)} expected_rows={len(expected_selected)} "
            f"differing_columns={differing_columns[:20]} "
            "(cell values suppressed to protect customer data)"
        ) from None


def _keyed(frame: pd.DataFrame, keys: list[str], label: str) -> pd.DataFrame:
    missing = [key for key in keys if key not in frame.columns]
    if missing:
        raise AssertionError(f"{label}: missing key column(s) {missing}")
    index = pd.MultiIndex.from_arrays(
        [frame[key].map(_normalize_key_value) for key in keys],
        names=keys,
    )
    if any(not value for level in range(len(keys)) for value in index.get_level_values(level)):
        raise AssertionError(f"{label}: empty key {keys}")
    duplicates = int(index.duplicated().sum())
    if duplicates:
        raise AssertionError(f"{label}: {duplicates} duplicate key(s) {keys}")
    return frame.set_axis(index)


def _revised_documents(actual: pd.DataFrame, expected: pd.DataFrame) -> pd.Index:
    """Documents whose fresh revision stamp is newer than the stored one."""
    newer = pd.Series(False, index=expected.index)
    for column in ORDER_REVISION_COLUMNS:
        if column not in expected.columns:
            continue
        stored = pd.to_datetime(actual[column], errors="coerce")
        fresh = pd.to_datetime(expected[column], errors="coerce")
        newer |= fresh.notna() & (stored.isna() | (fresh > stored))
    return expected.index[newer.to_numpy()]


def compare_order_frames(
    actual: dict[str, pd.DataFrame],
    expected: dict[str, pd.DataFrame],
    target_date: date,
) -> dict[str, int]:
    header_actual, header_expected = _aligned_partitions(actual, expected, "DonHang", target_date)
    detail_actual, detail_expected = _aligned_partitions(actual, expected, "ChiTietSP", target_date)

    header_actual = _keyed(header_actual, ["ma_phieu"], "DonHang SharePoint partition")
    header_expected = _keyed(header_expected, ["ma_phieu"], "DonHang fresh source")
    missing_documents = header_expected.index.difference(header_actual.index)
    extra_documents = header_actual.index.difference(header_expected.index)
    if len(missing_documents) or len(extra_documents):
        raise AssertionError(
            "Sheet DonHang: document set does not match fresh MobiWork source; "
            f"actual_rows={len(header_actual)} expected_rows={len(header_expected)} "
            f"missing_documents={len(missing_documents)} extra_documents={len(extra_documents)}"
        )
    header_actual = header_actual.loc[header_expected.index]

    immutable = [column for column in ORDER_IMMUTABLE_COLUMNS if column in header_expected.columns]
    _assert_partition_equal(
        header_actual[immutable], header_expected[immutable], "DonHang (identity columns)"
    )

    revised = _revised_documents(header_actual, header_expected)
    stable = header_expected.index.difference(revised, sort=False)
    _assert_partition_equal(header_actual.loc[stable], header_expected.loc[stable], "DonHang")

    detail_actual = _keyed(detail_actual, ["ma_phieu", "stt"], "ChiTietSP SharePoint partition")
    detail_expected = _keyed(detail_expected, ["ma_phieu", "stt"], "ChiTietSP fresh source")
    documents = set(header_expected.index.get_level_values("ma_phieu"))
    for label, frame in (("SharePoint partition", detail_actual), ("fresh source", detail_expected)):
        orphans = set(frame.index.get_level_values("ma_phieu")) - documents
        if orphans:
            raise AssertionError(
                f"Sheet ChiTietSP: {label} has lines for {len(orphans)} document(s) missing from DonHang"
            )

    revised_ids = set(revised.get_level_values("ma_phieu"))
    actual_stable = detail_actual.loc[~detail_actual.index.get_level_values("ma_phieu").isin(revised_ids)]
    expected_stable = detail_expected.loc[~detail_expected.index.get_level_values("ma_phieu").isin(revised_ids)]
    missing_lines = expected_stable.index.difference(actual_stable.index)
    extra_lines = actual_stable.index.difference(expected_stable.index)
    if len(missing_lines) or len(extra_lines):
        raise AssertionError(
            "Sheet ChiTietSP: line set does not match fresh MobiWork source; "
            f"actual_rows={len(detail_actual)} expected_rows={len(detail_expected)} "
            f"missing_lines={len(missing_lines)} extra_lines={len(extra_lines)}"
        )
    _assert_partition_equal(actual_stable.loc[expected_stable.index], expected_stable, "ChiTietSP")

    # A revised document may gain or drop lines, but losing all of them in
    # SharePoint is a write defect, not an edit.
    stored_line_documents = set(detail_actual.index.get_level_values("ma_phieu"))
    fresh_line_documents = set(detail_expected.index.get_level_values("ma_phieu"))
    lineless = (fresh_line_documents & revised_ids) - stored_line_documents
    if lineless:
        raise AssertionError(
            f"Sheet ChiTietSP: {len(lineless)} revised document(s) have no lines in SharePoint"
        )

    return {
        "compared_rows": len(header_expected) + len(detail_expected),
        "revised_documents": len(revised),
    }


def compare_report_frames(
    actual: dict[str, pd.DataFrame],
    expected: dict[str, pd.DataFrame],
    target_date: date,
    export_mode: str = "flat",
) -> dict[str, int]:
    if export_mode == "order":
        return compare_order_frames(actual, expected, target_date)

    compared_rows = 0
    for sheet_name in expected:
        actual_selected, expected_selected = _aligned_partitions(actual, expected, sheet_name, target_date)
        _assert_partition_equal(actual_selected, expected_selected, sheet_name)
        compared_rows += len(expected_selected)

    return {"compared_rows": compared_rows}


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
            comparison = compare_report_frames(actual, expected, target, cfg.export_mode)
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
        if comparison.get("revised_documents"):
            LOG.info(
                "report=%s: %s document(s) revised in MobiWork after the sync; identity re-checked",
                cfg.key,
                comparison["revised_documents"],
            )

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
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run_smoke()
