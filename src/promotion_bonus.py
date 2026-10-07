from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from excel_export import _format_sheet, _validate_excel_size
from mobiwork import MobiWorkClient
import promotion_bonus_ui as ui
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
        if type(self.enabled) is not bool:
            raise TypeError("enabled must be a boolean")
        if type(self.catalog_page_size) is not int:
            raise TypeError("catalog_page_size must be an integer")
        for field in ("name", "folder", "filename", "catalog_url", "report_url"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field).strip():
                raise ValueError(f"{field} must be a non-empty string")
        if any(part in {".", ".."} for part in self.folder.replace("\\", "/").split("/")):
            raise ValueError("folder must not contain traversal segments")
        if "/" in self.filename or "\\" in self.filename:
            raise ValueError("filename must be a basename")
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


def _optional_object_list(
    payload: dict[str, Any],
    key: str,
    operation: str,
) -> list[dict[str, Any]]:
    if payload.get(key) is None:
        return []
    return _expect_object_list(payload, key, operation)


def _api_total(payload: dict[str, Any], operation: str) -> int | None:
    value = payload.get("total")
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"{operation}: total must be an integer")
    try:
        total = int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{operation}: total is not an integer: {value!r}") from exc
    if total < 0:
        raise ValueError(f"{operation}: total must not be negative")
    return total


def _program_id(program: dict[str, Any]) -> str:
    value = program.get("_id")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def fetch_programs(
    client: MobiWorkClient,
    cfg: PromotionBonusConfig,
    *,
    filters: dict[str, str] | None = None,
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
                **(filters or {}),
                "page_size": cfg.catalog_page_size,
                "page_number": page,
            },
            operation_key="promotion_bonus_catalog",
            request_number=page,
        )
        page_total = _api_total(payload, "PromotionBonus catalogue")
        if expected_total is None:
            expected_total = page_total
        elif page_total is not None and page_total != expected_total:
            raise RuntimeError("PromotionBonus catalogue total changed during pagination")

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
        program_id = _program_id(program)
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
    if expected_total is not None and len(unique) != expected_total:
        raise RuntimeError(
            f"PromotionBonus catalogue API total={expected_total}, unique programs={len(unique)}"
        )
    return list(unique.values())


def _with_program_provenance(
    rows: list[dict[str, Any]],
    program: dict[str, Any],
) -> list[dict[str, Any]]:
    program_id = _program_id(program)
    program_name = str(program.get("name", "")).strip()
    return [
        {
            **row,
            "promotion_program_id": program_id,
            "promotion_program_name": program_name,
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
        program_id = _program_id(program)
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

        current_targets = _optional_object_list(
            payload,
            "arrChiTieu",
            f"PromotionBonusReport id_ct={program_id}",
        )
        current_rewards = _optional_object_list(
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
    def flatten(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in record.items():
            column = f"{prefix}_{key}" if prefix else str(key)
            cells = flatten(value, column) if isinstance(value, dict) and value else {column: value}
            if result.keys() & cells.keys():
                raise ValueError(f"{label}: nested column collision")
            result.update(cells)
        return result

    frame = pd.DataFrame([flatten(row) for row in records], dtype=object)
    if not frame.empty:
        for column in frame.columns:
            if frame[column].map(lambda value: isinstance(value, (dict, list, tuple, set))).any():
                frame[column] = frame[column].map(_excel_safe)
    if len(frame.columns) > 16_384:
        raise ValueError(f"{label}: exceeds Excel column limit")
    for column in frame.columns:
        if len(str(column)) > 32_767:
            raise ValueError(f"{label}: column name exceeds Excel cell limit")
        for value in frame[column]:
            if isinstance(value, str) and len(value) > 32_767:
                raise ValueError(f"{label}: value exceeds Excel cell limit")
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
    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".xlsx", delete=False) as handle:
        staged = Path(handle.name)
    try:
        with pd.ExcelWriter(staged, engine="openpyxl") as writer:
            for sheet_name, frame in frames.items():
                frame.to_excel(writer, sheet_name=sheet_name, index=False)
                # Source strings are data, including values beginning with '='.
                for row in writer.sheets[sheet_name].iter_rows():
                    for cell in row:
                        if cell.data_type == "f":
                            cell.data_type = "s"
                _format_sheet(writer, sheet_name)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)
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


SOURCES = {"auto", "calc", "ui", "openapi"}


def resolve_source() -> str:
    """auto -> compute from OpenAPI data (PromotionBonus + Bill + DisplayData).

    ``ui`` reads the DMS web page (needs web session secrets) and ``openapi`` keeps
    the legacy raw PromotionBonusReport snapshot.
    """
    requested = os.environ.get("PROMOTION_BONUS_SOURCE", "auto").strip().casefold() or "auto"
    if requested not in SOURCES:
        raise ValueError(f"PROMOTION_BONUS_SOURCE must be one of {sorted(SOURCES)}")
    return "calc" if requested == "auto" else requested


def _select_programs(programs: list[dict[str, Any]], selected: str) -> list[dict[str, Any]]:
    selected = (selected or "all").strip()
    if selected == "all":
        return programs
    wanted = {part.strip() for part in selected.split(",") if part.strip()}
    chosen = [p for p in programs if _program_id(p) in wanted]
    missing = wanted - {_program_id(p) for p in chosen}
    if missing:
        raise ValueError(f"Selected Promotion Bonus program(s) not in catalogue: {sorted(missing)}")
    return chosen


def _build_ui_workbook(
    client: MobiWorkClient,
    cfg: PromotionBonusConfig,
    manifest: dict[str, Any],
) -> Path:
    first, last = ui.report_period(
        os.environ.get("PROMOTION_BONUS_FROM_DATE", "").strip(),
        os.environ.get("PROMOTION_BONUS_TO_DATE", "").strip(),
    )
    sttt = os.environ.get("PROMOTION_BONUS_STTT", "").strip()
    filters = {"fromdate": first.strftime("%d/%m/%Y"), "todate": last.strftime("%d/%m/%Y")}
    programs = _select_programs(
        fetch_programs(client, cfg, filters=filters),
        os.environ.get("PROMOTION_BONUS_PROGRAM", "all"),
    )
    manifest.update({"from_date": first.isoformat(), "to_date": last.isoformat(),
                     "sttt": sttt, "program_count": len(programs)})
    results = ui.fetch_ui_snapshot(programs, first, last, sttt=sttt)
    counts = ui.snapshot_counts(results)
    manifest.update(counts)
    if counts["unresolved_programs"] and not _env_bool("PROMOTION_BONUS_ALLOW_PARTIAL", False):
        raise RuntimeError(
            f"{len(counts['unresolved_programs'])} program(s) still returned order envelopes "
            "after retries; refusing to publish an incomplete report"
        )
    manifest["phase"] = "workbook_build"
    return write_workbook(ui.build_ui_frames(results, first, last, sttt), cfg.filename)


def _bill_detail(first: Any, dry_run: bool, manifest: dict[str, Any]) -> pd.DataFrame:
    """Bill monthly master ChiTietSP for the report month (local file in dry runs)."""
    from io import BytesIO

    from data_cham_anh_export import _monthly_master_path
    from main import load_reports
    from monthly_master import master_filename

    bill = next(r for r in load_reports(ROOT / "config" / "reports.json") if r.key == "bill" and r.enabled)
    local = Path("output") / master_filename(bill.name, first)
    if dry_run and local.exists():
        content = local.read_bytes()
        manifest["bill_source"] = f"local:{local.name}"
    else:
        sharepoint = SemanticSharePointClient.from_env()
        drive = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip() or sharepoint.get_drive_id(
            sharepoint.get_site_id())
        remote = _monthly_master_path(bill, first)
        content = sharepoint.download_file_bytes(drive, remote)
        if not content:
            raise ValueError(f"Bill monthly master missing: {remote}")
        manifest["bill_source"] = f"sharepoint:{remote}"
    manifest["bill_sha256"] = hashlib.sha256(content).hexdigest()
    return pd.read_excel(BytesIO(content), sheet_name="ChiTietSP", dtype=object)


def _github_notice(title: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title={title}::{message}")


def _build_calc_workbook(
    client: MobiWorkClient,
    cfg: PromotionBonusConfig,
    manifest: dict[str, Any],
    dry_run: bool,
) -> tuple[Path, list[tuple[Path, str]]]:
    import promotion_bonus_calc as calc
    from customer_catalogue import enrich_customer_config
    from region_mapping import load_region_map

    first, last = ui.report_period(
        os.environ.get("PROMOTION_BONUS_FROM_DATE", "").strip(),
        os.environ.get("PROMOTION_BONUS_TO_DATE", "").strip(),
    )
    if (first.year, first.month) != (last.year, last.month):
        raise ValueError("Computed Promotion Bonus report covers one calendar month at a time")
    filters = {"fromdate": first.strftime("%d/%m/%Y"), "todate": last.strftime("%d/%m/%Y")}
    programs = _select_programs(
        fetch_programs(client, cfg, filters=filters),
        os.environ.get("PROMOTION_BONUS_PROGRAM", "all"),
    )
    manifest.update({"from_date": first.isoformat(), "to_date": last.isoformat(),
                     "program_count": len(programs)})
    lines = calc.sold_lines(_bill_detail(first, dry_run, manifest), first, last)
    manifest["sold_line_count"] = len(lines)
    customers = enrich_customer_config(client, {"customer_catalogue_start_date": "01/01/1900"})
    manifest["customer_catalogue_count"] = customers["customer_catalogue_audit"]["count"]
    displays: dict[tuple[str, str], str] = {}
    display_note = "Không có chương trình yêu cầu trưng bày"
    if any(isinstance(p.get("cttb"), dict) and p["cttb"].get("ten") for p in programs):
        try:
            records = calc.fetch_display_records(client, first, last)
            displays = calc.display_passes(records)
            display_note = f"DisplayData {len(records)} lượt chấm"
            manifest["display_record_count"] = len(records)
        except Exception as exc:  # display data is informative; never block the report
            display_note = f"Không lấy được DisplayData ({type(exc).__name__})"
            manifest["display_error"] = f"{type(exc).__name__}: {exc}"
    results, issues = calc.compute(
        programs, lines, customers["customer_catalogue"], displays=displays,
        region_of=calc.region_from_code(load_region_map(str(ROOT / "config" / "employee_regions.json"))),
    )
    counts = ui.snapshot_counts(results)
    diag = calc.diagnostics(results)
    manifest.update(counts)
    manifest.update({"rule_issues": issues, "program_diagnostics": diag,
                     "reached_rows": sum(d["reached"] for d in diag),
                     "eligible_rows": sum(d["eligible"] for d in diag)})
    _github_notice("Promotion Bonus (calc)",
                   f"{first:%m/%Y}: programs={len(programs)} rows={counts['customer_row_count']} "
                   f"reached={manifest['reached_rows']} eligible={manifest['eligible_rows']} "
                   f"sold_lines={len(lines)} display={display_note}")
    for item in diag:
        if item["registered"]:
            _github_notice("Promotion Bonus program",
                           f"{item['name'][:60]} | reg={item['registered']} sales={item['with_sales']} "
                           f"reached={item['reached']} display_ok={item['display_passed']} "
                           f"eligible={item['eligible']}")
    manifest["phase"] = "workbook_build"
    notes = [
        ("Quy tắc", "Khách đăng ký của chương trình; đơn bán (bỏ dòng khuyến mãi) có ngày giao trong kỳ; "
                    "chỉ cộng sản phẩm và đúng đơn vị khai trong chương trình; tiền = đơn giá × số lượng"),
        ("Khu vực", "Vùng áp dụng của chương trình (OpenAPI không có khu vực chi tiết của khách)"),
        ("Trưng bày", display_note),
        ("Đơn bán hàng", f"{len(lines)} dòng bán trong kỳ ({manifest.get('bill_source', '')})"),
    ]
    frames = ui.build_ui_frames(results, first, last, "", "Tính từ OpenAPI (PromotionBonus + Đơn bán hàng)",
                                notes)
    if issues:
        frames["Can_xem"] = pd.DataFrame(issues, dtype=object)
    path = write_workbook(frames, cfg.filename)
    monthly = write_workbook(frames, f"BaoCaoTraThuong_{first:%Y-%m}.xlsx")
    return path, [(monthly, f"{cfg.folder}/{first:%Y}/{first:%m}")]


def run() -> dict[str, Any]:
    dry_run = _env_bool("DRY_RUN", False)
    started_at = datetime.now(timezone.utc)
    manifest: dict[str, Any] = {
        "status": "running",
        "dataset": "promotion_bonus",
        "storage_mode": "current_snapshot",
        "historical_backfill_supported": False,
        "dry_run": dry_run,
        "phase": "config",
        "workbook_published": False,
        "started_at": started_at.isoformat(),
    }

    try:
        source = resolve_source()
        manifest["source"] = source
        if source == "openapi" and _env_bool("PROMOTION_BONUS_REQUIRE_DMS_MATCH", False):
            raise RuntimeError(
                "DMS-equivalent export blocked: report date parameters, calculation enum "
                "and region/customer schema have not been verified. "
                "Configure MOBIWORK_WEB_EMAIL/TOKENKEY/ALIAS to use the DMS web source."
            )
        cfg = load_config()
        manifest.update({"folder": cfg.folder, "filename": cfg.filename})
        if not cfg.enabled:
            manifest.update({
                "status": "skipped",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "reason": "config disabled",
            })
            _write_manifest(manifest)
            return manifest

        manifest["phase"] = "source_fetch"
        client = MobiWorkClient.from_env()
        extra_uploads: list[tuple[Path, str]] = []
        if source == "calc":
            path, extra_uploads = _build_calc_workbook(client, cfg, manifest, dry_run)
        elif source == "ui":
            path = _build_ui_workbook(client, cfg, manifest)
        else:
            programs = fetch_programs(client, cfg)
            snapshot = fetch_snapshot(client, cfg, programs)
            manifest["phase"] = "workbook_build"
            frames = build_frames(snapshot)
            path = write_workbook(frames, cfg.filename)
            manifest.update(
                {
                    "program_count": len(programs),
                    "report_request_count": len(programs),
                    "data_row_count": len(snapshot["data"]),
                    "target_row_count": len(snapshot["targets"]),
                    "reward_row_count": len(snapshot["rewards"]),
                }
            )
        content = path.read_bytes()
        manifest.update(
            {
                "workbook_sha256": hashlib.sha256(content).hexdigest(),
                "workbook_bytes": len(content),
            }
        )

        if not dry_run:
            manifest["phase"] = "workbook_publish"
            sharepoint = SemanticSharePointClient.from_env()
            drive_id = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
            if not drive_id:
                site_id = sharepoint.get_site_id()
                drive_id = sharepoint.get_drive_id(site_id)

            uploaded = sharepoint.upload_file(drive_id, path, cfg.folder)
            for extra_path, extra_folder in extra_uploads:
                sharepoint.upload_file(drive_id, extra_path, extra_folder)
                manifest.setdefault("extra_published", []).append(f"{extra_folder}/{extra_path.name}")
            manifest["workbook_published"] = True
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
        if not dry_run:
            manifest["phase"] = "state_publish"
            sharepoint.upload_json(
                drive_id,
                f"{cfg.folder}/_sync_state/promotion_bonus.json",
                {**manifest, "phase": "complete"},
            )
        manifest["phase"] = "complete"
        _write_manifest(manifest)
        LOG.info(
            "Promotion Bonus snapshot complete source=%s programs=%s rows=%s",
            source,
            manifest.get("program_count"),
            manifest.get("customer_row_count", manifest.get("data_row_count")),
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
        if os.environ.get("GITHUB_ACTIONS") == "true":
            detail = " ".join(f"{type(exc).__name__}: {exc}".split())[:500]
            print(f"::error title=Promotion Bonus failed ({manifest.get('phase')})::{detail}")
        raise


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run()
