"""Export only the bounded real-image scoring sample for human visual audit.

This utility is intentionally probe-only. It does not change scoring decisions,
model assets, caches, canonical KPI files, or SharePoint image storage. The main
sample workflow runs it after scoring so the private GitHub artifact contains the
same images that were actually evaluated.
"""
from __future__ import annotations

import argparse
import os
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pandas as pd

import main as core
from image_storage import ImageSharePointClient
from image_sync import ImageSyncConfig
from scoring.records import assign_record_ids
from sharepoint_kpi_source import SharePointMonthlyKPISource

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
RETRYABLE_STATUSES = frozenset({"PENDING_SCORE", "TECHNICAL_FAILURE"})
_ALLOWED_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
)


def _safe_token(value: object, fallback: str = "x") -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", text).strip("._-")
    return text[:80] or fallback


def _scored_detail_rows(detail: pd.DataFrame) -> pd.DataFrame:
    if "record_id" not in detail.columns or "Trạng Thái Quyết Định" not in detail.columns:
        raise ValueError("Detail CSV missing record_id / Trạng Thái Quyết Định")
    statuses = detail["Trạng Thái Quyết Định"].astype(str).str.strip().str.upper()
    record_ids = detail["record_id"].astype(str).str.strip()
    selected = detail.loc[record_ids.ne("") & ~statuses.isin(RETRYABLE_STATUSES)].copy()
    if selected["record_id"].duplicated().any():
        raise ValueError("Scored sample contains duplicate record_id values")
    return selected


def _suffix(remote_path: str) -> str:
    suffix = PurePosixPath(remote_path).suffix.casefold()
    return suffix if suffix in _ALLOWED_SUFFIXES else ".jpg"


def export_sample_images(period: str, detail_path: Path, output_dir: Path) -> Path:
    parsed = pd.Period(period, freq="M")
    current = datetime(parsed.year, parsed.month, 15, 12, tzinfo=VN_TZ)
    detail = pd.read_csv(detail_path, dtype=str, keep_default_na=False)
    scored = _scored_detail_rows(detail)
    if scored.empty:
        raise RuntimeError("No scored rows available for visual audit")

    drive_id = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
    if not drive_id:
        raise RuntimeError("SHAREPOINT_DRIVE_ID is required for sample image export")
    client = ImageSharePointClient.from_env()
    source = SharePointMonthlyKPISource(client, drive_id, core.enabled_reports())
    inputs = source.load(current)
    source_rows = source.recent_image_rows(inputs.visits, current)
    record_ids = assign_record_ids(source_rows)
    by_record_id = {record_id: row for record_id, row in zip(record_ids, source_rows, strict=True)}

    output_dir.mkdir(parents=True, exist_ok=True)
    cfg = ImageSyncConfig.from_env()
    audit_rows: list[dict[str, object]] = []
    missing: list[str] = []

    for ordinal, record in enumerate(scored.to_dict(orient="records"), start=1):
        record_id = str(record.get("record_id") or "").strip()
        source_row = by_record_id.get(record_id)
        if source_row is None:
            missing.append(f"{record_id}: source row not found")
            continue
        try:
            remote_path = source.resolve_image_path(source_row, cfg)
            content = client.download_file_bytes(drive_id, remote_path)
            if not content:
                raise FileNotFoundError(remote_path)
        except Exception as error:
            missing.append(f"{record_id}: {type(error).__name__}: {error}")
            continue

        label = _safe_token(record.get("Phân Loại AI"), "label")
        status = _safe_token(record.get("Trạng Thái Quyết Định"), "status")
        customer = _safe_token(record.get("ma_kh"), "no_customer")
        filename = (
            f"{ordinal:03d}__{label}__{status}__{customer}__{record_id[:12]}"
            f"{_suffix(remote_path)}"
        )
        local_path = output_dir / filename
        local_path.write_bytes(content)
        audit_rows.append(
            {
                "ordinal": ordinal,
                "record_id": record_id,
                "ma_kh": record.get("ma_kh", ""),
                "ten_kh": record.get("ten_kh", ""),
                "ten_nhan_vien": record.get("ten_nhan_vien", ""),
                "ngay": record.get("ngay", ""),
                "stt_hinh": record.get("stt_hinh", ""),
                "ai_label": record.get("Phân Loại AI", ""),
                "decision_status": record.get("Trạng Thái Quyết Định", ""),
                "scene": record.get("Loại Cảnh", ""),
                "pass_score": record.get("Điểm Pass", ""),
                "fraud_score": record.get("Điểm Fraud", ""),
                "reference_similarity": record.get("Độ Tương Đồng Mẫu", ""),
                "nearest_references": record.get("3 Tham Chiếu Gần Nhất", ""),
                "detector_evidence": record.get("Bằng Chứng Detector", ""),
                "source_url": record.get("hinh_anh", ""),
                "remote_path": remote_path,
                "artifact_file": filename,
            }
        )

    if missing:
        raise RuntimeError("Sample image export incomplete:\n" + "\n".join(missing[:20]))
    if len(audit_rows) != len(scored):
        raise RuntimeError(
            f"Sample image export count mismatch: exported={len(audit_rows)} scored={len(scored)}"
        )

    manifest_path = output_dir / "sample_images_manifest.csv"
    pd.DataFrame(audit_rows).to_csv(manifest_path, index=False, encoding="utf-8-sig")
    print(f"visual_audit_images={len(audit_rows)}")
    print(f"visual_audit_manifest={manifest_path}")
    return manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", required=True, help="YYYY-MM")
    parser.add_argument("--detail", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export_sample_images(args.period, args.detail, args.output)


if __name__ == "__main__":
    main()
