from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from excel_export import _format_sheet, _validate_excel_size
from mobiwork import MobiWorkClient
from sharepoint_semantic import SemanticSharePointClient


LOG = logging.getLogger("mobiwork_promotion_bonus")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "promotion_bonus.json"
MANIFEST_PATH = Path("output") / "promotion_bonus_manifest.json"


@dataclass(frozen=True)
class PromotionBonusConfig:
    enabled: bool
    name: str
    folder: str
    filename: str
    catalog_url: str
    report_url: str
    catalog_page_size: int = 200

    def __post_init__(self) -> None:
        if self.catalog_page_size < 1 or self.catalog_page_size > 200:
            raise ValueError("catalog_page_size must be between 1 and 200")
        if not self.folder.strip():
            raise ValueError("Promotion Bonus folder must not be empty")
        if not self.filename.lower().endswith(".xlsx"):
            raise ValueError("Promotion Bonus filename must be an .xlsx workbook")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> PromotionBonusConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("config/promotion_bonus.json must contain a JSON object")
    return PromotionBonusConfig(**payload)


def _expect_object_list(payload: dict[str, Any], key: str, operation: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if value is None:
        raise ValueError(f"{operation}: response is missing {key!r}")
    if not isinstance(value, list):
        raise TypeError(f"{operation}: {key!r} must be an array")
    invalid = [type(item).__name__ for item in value if not isinstance(item, dict)]
    if invalid:
        raise TypeError(
            f"{operation}: {key!r} contains non-object values: "
            f"{', '.join(sorted(set(invalid)))}"
        )
    return value


def _api_total(payload: dict[str, Any], operation: str) -> int | None:
    value = payload.get("total")
    if value in (None, ""):
        return None
    try:
        total = int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{operation}: total is not an integer: {value!r}") from exc
    if total < 0:
        raise ValueError(f"{operation}: total must not be negative")
    return total


def fetch_programs(
    client: MobiWorkClient,
    cfg: PromotionBonusConfig,
) -> list[dict[str, Any]]:
    """Fetch the complete PromotionBonus catalogue before requesting report snapshots."""
    records: list[dict[str, Any]] = []
    expected_total: int | None = None
    seen_pages: set[str] = set()
    page = 1

    while True:
        payload = client.get_json(
            cfg.catalog_url,
            {
                "page_size": cfg.catalog_page_size,
                "page_number": page,
            },
            operation_key="promotion_bonus_catalog",
            request_number=page,
        )
        if expected_total is None:
            expected_total = _api_total(payload, "PromotionBonus catalogue")

        page_rows = _expect_object_list(payload, "data", "PromotionBonus catalogue")
        if page_rows:
            signature = json.dumps(
                page_rows,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            )
            if signature in seen_pages:
                raise RuntimeError(
                    f"PromotionBonus catalogue repeated page {page}; refusing incomplete pagination"
                )
            seen_pages.add(signature)

        records.extend(page_rows)
        if expected_total is not None and len(records) >= expected_total:
            break
        if not page_rows:
            break

        page += 1
        if page > 10_000:
            raise RuntimeError("PromotionBonus catalogue pagination safety limit exceeded")

    if expected_total is not None and len(records) != expected_total:
        raise RuntimeError(
            f"PromotionBonus catalogue API total={expected_total}, fetched={len(records)}"
        )

    unique: dict[str, dict[str, Any]] = {}
    for row_number, program in enumerate(records, start=1):
        program_id = str(program.get("_id", "")).strip()
        if not program_id:
            raise ValueError(
                f"PromotionBonus catalogue row {row_number} is missing required _id"
            )
        previous = unique.get(program_id)
        if previous is not None and previous != program:
            raise ValueError(
                f"PromotionBonus catalogue has conflicting duplicate _id={program_id}"
            )
        unique[program_id] = program
    return list(unique.values())


def _with_program_provenance(
    rows: list[dict[str, Any]],
    program: dict[str, Any],
) -> list[dict[str, Any]]:
    program_id = str(program.get("_id", "")).strip()
    program_name = str(program.get("name", "")).strip()
    return [
        {
            "promotion_program_id": program_id,
            "promotion_program_name": program_name,
            **row,
        }
        for row in rows
    ]


def fetch_snapshot(
    client: MobiWorkClient,
    cfg: PromotionBonusConfig,
    programs: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Fetch one authoritative current snapshot per program.

    The report endpoint permits up to five ids but does not document a stable field that
    maps every returned row back to one id. One request per program preserves exact
    provenance and avoids mixing reward rows between programs.
    """
    data_rows: list[dict[str, Any]] = []
    target_rows: list[dict[str, Any]] = []
    reward_rows: list[dict[str, Any]] = []

    for request_number, program in enumerate(programs, start=1):
        program_id = str(program.get("_id", "")).strip()
        if not program_id:
            raise ValueError("PromotionBonus report cannot run without a program _id")

        payload = client.get_json(
            cfg.report_url,
            {"id_ct": program_id},
            operation_key="promotion_bonus_report",
            request_number=request_number,
        )
        current_data = _expect_object_list(
            payload,
            "data",
            f"PromotionBonusReport id_ct={program_id}",
        )
        expected_total = _api_total(payload, f"PromotionBonusReport id_ct={program_id}")
        if expected_total is not None and len(current_data) != expected_total:
            raise RuntimeError(
                f"PromotionBonusReport id_ct={program_id}: "
                f"API total={expected_total}, fetched={len(current_data)}"
            )

        current_targets = _expect_object_list(
            payload,
            "arrChiTieu",
            f"PromotionBonusReport id_ct={program_id}",
        )
        current_rewards = _expect_object_list(
            payload,
            "arrTraThuong",
            f"PromotionBonusReport id_ct={program_id}",
        )

        data_rows.extend(_with_program_provenance(current_data, program))
        target_rows.extend(_with_program_provenance(current_targets, program))
        reward_rows.extend(_with_program_provenance(current_rewards, program))

    return {
        "programs": programs,
        "data": data_rows,
        "targets": target_rows,
        "rewards": reward_rows,
    }


def _excel_safe(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


def _frame(records: list[dict[str, Any]], label: str) -> pd.DataFrame:
    frame = pd.json_normalize(records, sep="_") if records else pd.DataFrame()
    if not frame.empty:
        for column in frame.columns:
            if frame[column].map(lambda value: isinstance(value, (dict, list, tuple, set))).any():
                frame[column] = frame[column].map(_excel_safe)
    _validate_excel_size(frame, label)
    return frame


def build_frames(snapshot: dict[str, list[dict[str, Any]]]) -> dict[str, pd.DataFrame]:
    return {
        "ChuongTrinh": _frame(snapshot["programs"], "ChuongTrinh"),
        "Data": _frame(snapshot["data"], "Data"),
        "ChiTieu": _frame(snapshot["targets"], "ChiTieu"),
        "TraThuong": _frame(snapshot["rewards"], "TraThuong"),
    }


def write_workbook(
    frames: dict[str, pd.DataFrame],
    filename: str,
    output_dir: Path = Path("output"),
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet_name, frame in frames.items():
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
            _format_sheet(writer, sheet_name)
    return path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _write_manifest(payload: dict[str, Any]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def run() -> dict[str, Any]:
    cfg = load_config()
    dry_run = _env_bool("DRY_RUN", False)
    started_at = datetime.now(timezone.utc)
    manifest: dict[str, Any] = {
        "status": "running",
        "dataset": "promotion_bonus",
        "storage_mode": "current_snapshot",
        "historical_backfill_supported": False,
        "dry_run": dry_run,
        "started_at": started_at.isoformat(),
        "folder": cfg.folder,
        "filename": cfg.filename,
    }

    if not cfg.enabled:
        manifest.update(
            {
                "status": "skipped",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "reason": "config disabled",
            }
        )
        _write_manifest(manifest)
        return manifest

    try:
        client = MobiWorkClient.from_env()
        programs = fetch_programs(client, cfg)
        snapshot = fetch_snapshot(client, cfg, programs)
        frames = build_frames(snapshot)
        path = write_workbook(frames, cfg.filename)
        content = path.read_bytes()

        manifest.update(
            {
                "program_count": len(programs),
                "report_request_count": len(programs),
                "data_row_count": len(snapshot["data"]),
                "target_row_count": len(snapshot["targets"]),
                "reward_row_count": len(snapshot["rewards"]),
                "workbook_sha256": hashlib.sha256(content).hexdigest(),
                "workbook_bytes": len(content),
            }
        )

        if not dry_run:
            sharepoint = SemanticSharePointClient.from_env()
            drive_id = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
            if not drive_id:
                site_id = sharepoint.get_site_id()
                drive_id = sharepoint.get_drive_id(site_id)

            uploaded = sharepoint.upload_file(drive_id, path, cfg.folder)
            manifest.update(
                {
                    "sharepoint_write_avoided": bool(uploaded.get("upload_skipped")),
                    "verification_mode": uploaded.get("verification_mode"),
                    "semantic_match": uploaded.get("semantic_match"),
                    "web_url": uploaded.get("webUrl"),
                }
            )

        manifest.update(
            {
                "status": "success",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _write_manifest(manifest)

        if not dry_run:
            sharepoint.upload_json(
                drive_id,
                f"{cfg.folder}/_sync_state/promotion_bonus.json",
                manifest,
            )
        LOG.info(
            "Promotion Bonus snapshot complete programs=%s data=%s targets=%s rewards=%s",
            len(programs),
            len(snapshot["data"]),
            len(snapshot["targets"]),
            len(snapshot["rewards"]),
        )
        return manifest
    except Exception as exc:
        manifest.update(
            {
                "status": "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        _write_manifest(manifest)
        raise


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run()
