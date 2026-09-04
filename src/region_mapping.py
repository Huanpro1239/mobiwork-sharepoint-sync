from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("config/employee_regions.json")
_PREFIX_RE = re.compile(r"^([A-Za-z]+)")
LOG = logging.getLogger("mobiwork_sync")


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def employee_prefix(ma_nv: Any) -> str | None:
    value = str(ma_nv or "").strip().upper()
    if not value:
        return None
    match = _PREFIX_RE.match(value)
    return match.group(1) if match else None


@lru_cache(maxsize=8)
def load_region_map(config_path: str | None = None) -> dict[str, dict[str, str]]:
    path = Path(config_path or os.environ.get("EMPLOYEE_REGION_CONFIG", "") or DEFAULT_CONFIG_PATH)
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_regions = payload.get("regions")
    if not isinstance(raw_regions, dict) or not raw_regions:
        raise ValueError(f"Employee region config {path} must contain a non-empty regions object")

    normalized: dict[str, dict[str, str]] = {}
    for raw_prefix, raw_region in raw_regions.items():
        prefix = str(raw_prefix).strip().upper()
        if not prefix or not prefix.isalpha():
            raise ValueError(f"Invalid employee prefix in {path}: {raw_prefix!r}")
        if not isinstance(raw_region, dict):
            raise TypeError(f"Region mapping for {prefix} must be an object")
        vung_code = str(raw_region.get("vung_code", "")).strip()
        vung = str(raw_region.get("vung", "")).strip()
        if not vung_code or not vung:
            raise ValueError(f"Region mapping for {prefix} must contain vung_code and vung")
        normalized[prefix] = {"vung_code": vung_code, "vung": vung}
    return normalized


def enrich_visit_records(
    records: list[dict[str, Any]],
    *,
    region_map: dict[str, dict[str, str]] | None = None,
    strict: bool | None = None,
) -> list[dict[str, Any]]:
    """Attach DMS sales region to visit rows using employee code, never customer type.

    ``loai_kh`` is customer classification and can cross an employee's assigned sales
    region. Region therefore comes only from ``ma_nv`` -> employee prefix -> region.

    Production defaults to non-strict mode so a newly introduced employee prefix never
    causes an otherwise valid Visit partition/month to disappear. Unknown employees are
    preserved explicitly as ``UNMAPPED / Chưa phân vùng`` and logged for mapping cleanup.
    Strict mode remains available for validation/tests through ``EMPLOYEE_REGION_STRICT``.
    """
    mapping = region_map or load_region_map()
    is_strict = _env_bool("EMPLOYEE_REGION_STRICT", False) if strict is None else strict
    enriched: list[dict[str, Any]] = []
    unmapped: dict[str, str] = {}

    for source in records:
        row = dict(source)
        ma_nv = str(row.get("ma_nv") or "").strip()
        prefix = employee_prefix(ma_nv)
        region = mapping.get(prefix or "")
        if region:
            row["vung_code"] = region["vung_code"]
            row["vung"] = region["vung"]
            row["vung_source"] = "ma_nv_prefix"
        else:
            row["vung_code"] = "UNMAPPED"
            row["vung"] = "Chưa phân vùng"
            row["vung_source"] = "unmapped"
            unmapped[ma_nv or "<missing ma_nv>"] = prefix or "<no prefix>"
        enriched.append(row)

    if unmapped:
        sample = ", ".join(
            f"{employee}({prefix})" for employee, prefix in sorted(unmapped.items())[:20]
        )
        LOG.warning(
            "Visit employee-region mapping incomplete; preserving %s row source(s) as "
            "UNMAPPED. Sample employees/prefixes: %s",
            len(unmapped),
            sample,
        )
        if is_strict:
            raise ValueError(
                "Visit employee-region mapping is incomplete. "
                f"Unmapped employees/prefixes: {sample}. "
                "Update config/employee_regions.json; loai_kh is intentionally not used as fallback."
            )
    return enriched
