"""End-to-end SharePoint image scoring + rolling Sales KPI pipeline."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pandas as pd

import main as core
from image_storage import ImageSharePointClient
from image_sync import ImageSyncConfig
from kpi.customer_aggregator import aggregate_customer_kpi
from kpi.customer_history import (
    apply_history_to_kpi,
    build_customer_history_status,
    history_csv_bytes,
)
from kpi.kpi_exporter import KPIExporter
from project_paths import (
    OUTPUT_DIR,
    OUTPUT_EXCEL,
    SCORE_CACHE_DB,
    TEMPLATE_EXCEL,
    ensure_runtime_dirs,
)
from scoring.assets import SharePointAssetManager
from scoring.records import assign_record_ids, build_audit_record, technical_failure_payload
from scoring.score_cache import ScoreCache
from sharepoint_kpi_source import SharePointMonthlyKPISource

LOG = logging.getLogger("mobiwork_sync")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


@dataclass(frozen=True)
class PipelineRunResult:
    manifest_path: Path
    workbook_path: Path
    detail_csv_path: Path
    remote_workbook_path: str
    status: str


def _resolve_drive(client: ImageSharePointClient) -> str:
    configured = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
    if configured:
        return configured
    return client.get_drive_id(client.get_site_id())


def _checkpoint_remote_path(root: str, period: pd.Timestamp) -> str:
    return f"{root}/{period:%Y-%m}/scoring_checkpoint.csv"


def _checkpoint_manifest_remote_path(root: str, period: pd.Timestamp) -> str:
    return f"{root}/{period:%Y-%m}/scoring_checkpoint_manifest.json"


def _read_remote_score_csv(client, drive_id: str, remote: str) -> list[dict[str, object]]:
    content = client.download_file_bytes(drive_id, remote)
    if not content:
        return []
    try:
        frame = pd.read_csv(BytesIO(content), dtype=str, keep_default_na=False)
    except Exception as error:
        LOG.warning("Cannot seed score cache from %s: %s", remote, error)
        return []
    return frame.to_dict(orient="records")


def _load_remote_score_rows(
    client, drive_id: str, period: pd.Timestamp
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    root = os.environ.get("KPI_SHAREPOINT_ROOT", "KPI").strip().strip("/") or "KPI"
    previous = period - pd.offsets.MonthBegin(1)
    remotes = (
        f"{root}/{previous:%Y-%m}/Ket_qua_Chi_tiet_Anh.csv",
        f"{root}/{period:%Y-%m}/Ket_qua_Chi_tiet_Anh.csv",
        _checkpoint_remote_path(root, period),
    )
    for remote in remotes:
        rows.extend(_read_remote_score_csv(client, drive_id, remote))
    return rows


def _checkpoint_frame(
    result_frame: pd.DataFrame, pipeline_signature: str
) -> pd.DataFrame:
    """Keep only reusable current-model rows for the resumable checkpoint."""

    if result_frame.empty:
        return result_frame.copy()
    signature = result_frame.get("pipeline_signature", pd.Series(dtype=str)).astype(str)
    image_sha = result_frame.get("image_sha256", pd.Series(dtype=str)).astype(str)
    mask = signature.eq(pipeline_signature) & image_sha.str.strip().ne("")
    checkpoint = result_frame.loc[mask].copy()
    if "hinh_anh" in checkpoint.columns:
        checkpoint = checkpoint.drop_duplicates(subset=["hinh_anh"], keep="last")
    return checkpoint


def _download_previous_kpi(client, drive_id: str, remote_path: str) -> Path | None:
    content = client.download_file_bytes(drive_id, remote_path)
    if not content:
        return None
    prior = OUTPUT_DIR / "previous_kpi.xlsx"
    prior.parent.mkdir(parents=True, exist_ok=True)
    prior.write_bytes(content)
    return prior


def _validate_assets() -> None:
    from scoring.config import REFERENCE_DIR, REFERENCE_OVERRIDES, YOLO_WEIGHTS

    if not REFERENCE_DIR.is_dir():
        raise FileNotFoundError(f"AI reference directory is missing: {REFERENCE_DIR}")
    if not REFERENCE_OVERRIDES.is_file():
        raise FileNotFoundError(f"AI reference override registry is missing: {REFERENCE_OVERRIDES}")
    if not YOLO_WEIGHTS.is_file():
        raise FileNotFoundError(f"YOLO weights are missing: {YOLO_WEIGHTS}")
    if not TEMPLATE_EXCEL.is_file():
        raise FileNotFoundError(f"KPI template is missing: {TEMPLATE_EXCEL}")


def _download_image_rows(source, client, drive_id: str, rows: list[dict[str, object]]):
    cfg = ImageSyncConfig.from_env()
    results: list[tuple[str | None, bytes | None, Exception | None]] = [
        (None, None, None)
    ] * len(rows)
    max_workers = max(1, int(os.environ.get("AI_DOWNLOAD_WORKERS", "8")))

    def fetch(index: int, row: dict[str, object]):
        remote = source.resolve_image_path(row, cfg)
        content = client.download_file_bytes(drive_id, remote)
        if not content:
            raise FileNotFoundError(remote)
        return index, remote, content

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch, index, row): index for index, row in enumerate(rows)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                _, remote, content = future.result()
                results[index] = (remote, content, None)
            except Exception as error:
                results[index] = (None, None, error)
    return results


def _build_image_results(
    source,
    client,
    drive_id: str,
    rows: list[dict[str, object]],
    period_start: pd.Timestamp,
):
    from scoring.service import ImageScoringService

    record_ids = assign_record_ids(rows)
    downloads = _download_image_rows(source, client, drive_id, rows)
    output: list[dict[str, object] | None] = [None] * len(rows)
    valid_indices = [
        i
        for i, (_remote, content, error) in enumerate(downloads)
        if content is not None and error is None
    ]
    stats = {
        "images": len(rows),
        "stored_images_loaded": len(valid_indices),
        "missing_or_failed_images": len(rows) - len(valid_indices),
        "remote_seeded_scores": 0,
        "cache_hits": 0,
        "new_unique_scores": 0,
        "production_pending_remaining": 0,
    }

    cache = ScoreCache(SCORE_CACHE_DB)
    with ImageScoringService(cache=cache) as service:
        signature = service.pipeline_signature
        stats["remote_seeded_scores"] = cache.seed(
            _load_remote_score_rows(client, drive_id, period_start), signature
        )
        if valid_indices:
            contents = [downloads[i][1] for i in valid_indices]
            before = sum(
                cache.get(signature, hashlib.sha256(content).hexdigest()) is not None
                for content in contents
                if content is not None
            )
            scored = service.score_contents(
                content for content in contents if content is not None
            )
            stats["cache_hits"] = sum(int(item.cache_hit) for item in scored)
            stats["new_unique_scores"] = len(
                {item.image_sha256 for item in scored if not item.cache_hit}
            )
            stats["preexisting_local_cache_matches"] = int(before)
            for index, outcome in zip(valid_indices, scored, strict=True):
                remote = downloads[index][0]
                row = rows[index]
                record = build_audit_record(
                    row,
                    record_ids[index],
                    signature,
                    outcome.image_sha256,
                    outcome.payload,
                )
                if remote:
                    record["Tên File"] = PurePosixPath(remote).name
                output[index] = record

        for index, (_remote, _content, error) in enumerate(downloads):
            if output[index] is not None:
                continue
            payload = technical_failure_payload(
                f"IMAGE_SOURCE_ERROR {type(error).__name__}: {error}"
                if error
                else "Ảnh SharePoint không khả dụng"
            )
            output[index] = build_audit_record(
                rows[index], record_ids[index], signature, "", payload
            )

    if any(item is None for item in output):
        raise RuntimeError("Image scoring left unresolved result rows")
    return pd.DataFrame(item for item in output if item is not None), stats, signature


def run(period: str | None = None, dry_run: bool = False) -> PipelineRunResult:
    ensure_runtime_dirs()
    current = datetime.now(VN_TZ)
    if period:
        parsed = pd.Period(period, freq="M")
        current = datetime(parsed.year, parsed.month, 15, 12, tzinfo=VN_TZ)
    period_start = pd.Timestamp(year=current.year, month=current.month, day=1)

    client = ImageSharePointClient.from_env()
    drive_id = _resolve_drive(client)
    if os.environ.get("AI_SYNC_ASSETS", "true").strip().casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }:
        asset_result = SharePointAssetManager(client, drive_id).sync_required_assets()
        LOG.info(
            "AI assets synced: downloaded=%s skipped=%s removed=%s",
            asset_result.downloaded,
            asset_result.skipped,
            asset_result.removed,
        )
    _validate_assets()

    reports = core.enabled_reports()
    source = SharePointMonthlyKPISource(client, drive_id, reports)

    history_status, inputs = build_customer_history_status(
        source, client, drive_id, current
    )
    recent_kpi_result = aggregate_customer_kpi(inputs.visits, inputs.orders, now=current)
    kpi_result = apply_history_to_kpi(recent_kpi_result, history_status.history)
    image_rows = source.recent_image_rows(inputs.visits, current)

    if image_rows:
        result_frame, scoring_stats, pipeline_signature = _build_image_results(
            source, client, drive_id, image_rows, period_start
        )
    else:
        pipeline_signature = "NO_IMAGE_ROWS"
        scoring_stats = {
            "images": 0,
            "stored_images_loaded": 0,
            "missing_or_failed_images": 0,
            "remote_seeded_scores": 0,
            "cache_hits": 0,
            "new_unique_scores": 0,
            "production_pending_remaining": 0,
        }
        result_frame = pd.DataFrame(
            columns=(
                "record_id",
                "pipeline_signature",
                "ten_nhan_vien",
                "ngay",
                "ma_kh",
                "ten_kh",
                "stt_hinh",
                "hinh_anh",
                "ghi_chu",
                "Tên File",
                "image_sha256",
                "Phân Loại AI",
                "Độ Tin Cậy AI",
                "Căn Cứ Nhận Diện",
                "Nội Dung Chữ OCR",
                "Trạng Thái Quyết Định",
                "Loại Cảnh",
                "Điểm Scene",
                "Điểm Pass",
                "Điểm Fraud",
                "Độ Tương Đồng Mẫu",
                "3 Tham Chiếu Gần Nhất",
                "Bằng Chứng Detector",
                "Quality Gate",
                "score_payload_json",
            )
        )

    root = os.environ.get("KPI_SHAREPOINT_ROOT", "KPI").strip().strip("/") or "KPI"
    remote_folder = f"{root}/{period_start:%Y-%m}"
    remote_workbook = f"{remote_folder}/Ket_qua_cham_cong_va_thuong_KPI.xlsx"
    remote_detail = f"{remote_folder}/Ket_qua_Chi_tiet_Anh.csv"
    remote_manifest = f"{remote_folder}/run_manifest.json"
    remote_checkpoint = _checkpoint_remote_path(root, period_start)
    remote_checkpoint_manifest = _checkpoint_manifest_remote_path(root, period_start)

    prior = _download_previous_kpi(client, drive_id, remote_workbook)
    exporter = KPIExporter(
        template_path=TEMPLATE_EXCEL,
        output_path=OUTPUT_EXCEL,
        manual_label_source_path=prior if prior is not None else OUTPUT_EXCEL,
        announce=False,
    )
    all_warnings = (
        tuple(inputs.warnings)
        + tuple(history_status.warnings)
        + tuple(kpi_result.warnings)
    )
    exporter.export_full_workbook(
        result_frame,
        customer_frame=kpi_result.customers,
        period_start=kpi_result.period_start,
        kpi_warnings=all_warnings,
    )

    detail_csv = OUTPUT_DIR / "Ket_qua_Chi_tiet_Anh.csv"
    result_frame.to_csv(detail_csv, index=False, encoding="utf-8-sig")

    pending_remaining = int(scoring_stats.get("production_pending_remaining") or 0)
    run_status = "warming_up" if not dry_run and pending_remaining > 0 else "success"
    manifest = {
        "schema_version": 4,
        "status": run_status,
        "period": period_start.strftime("%Y-%m"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scoring_pipeline_version": "2.3.0",
        "kpi_rules_version": "2.4.0",
        "customer_history_schema_version": "1.0",
        "pipeline_signature": pipeline_signature,
        "scoring": scoring_stats,
        "customer_history": {
            "remote_path": history_status.remote_path,
            "rows": len(history_status.history),
            "initialized_now": history_status.initialized_now,
            "bootstrap_source_files": history_status.bootstrap_source_files,
            "incremental_source_files": history_status.incremental_source_files,
        },
        "customers_in_kpi": len(kpi_result.customers),
        "employees_in_kpi": int(kpi_result.customers["ten_nhan_vien"].nunique())
        if not kpi_result.customers.empty
        else 0,
        "visit_source_files": len(inputs.visit_sources),
        "order_source_files": len(inputs.order_sources),
        "history_start": str(kpi_result.history_start.date())
        if kpi_result.history_start is not None
        else None,
        "warnings": list(all_warnings),
        "dry_run": bool(dry_run),
        "remote_outputs": {
            "workbook": remote_workbook,
            "detail_csv": remote_detail,
            "manifest": remote_manifest,
            "customer_history": history_status.remote_path,
            "scoring_checkpoint": remote_checkpoint,
            "scoring_checkpoint_manifest": remote_checkpoint_manifest,
        },
    }
    if run_status == "warming_up":
        manifest["warnings"].append(
            f"AI production catch-up còn {pending_remaining:,} ảnh; KPI chính chưa bị ghi đè."
        )

    manifest_path = OUTPUT_DIR / "run_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    if not dry_run:
        if run_status == "warming_up":
            checkpoint = _checkpoint_frame(result_frame, pipeline_signature)
            checkpoint_csv = OUTPUT_DIR / "scoring_checkpoint.csv"
            checkpoint.to_csv(checkpoint_csv, index=False, encoding="utf-8-sig")
            client.upload_bytes(
                drive_id,
                remote_checkpoint,
                checkpoint_csv.read_bytes(),
                "text/csv; charset=utf-8",
            )
            client.upload_json(drive_id, remote_checkpoint_manifest, manifest)
            LOG.warning(
                "Production scoring warm-up checkpoint saved: rows=%s remaining=%s; final KPI untouched",
                len(checkpoint),
                pending_remaining,
            )
        else:
            client.upload_file(drive_id, OUTPUT_EXCEL, remote_folder)
            client.upload_bytes(
                drive_id,
                remote_detail,
                detail_csv.read_bytes(),
                "text/csv; charset=utf-8",
            )
            client.upload_bytes(
                drive_id,
                history_status.remote_path,
                history_csv_bytes(history_status.history),
                "text/csv; charset=utf-8",
            )
            client.upload_json(drive_id, remote_manifest, manifest)

    return PipelineRunResult(
        manifest_path=manifest_path,
        workbook_path=OUTPUT_EXCEL,
        detail_csv_path=detail_csv,
        remote_workbook_path=remote_workbook,
        status=run_status,
    )
