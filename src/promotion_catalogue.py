"""Promotion programme configuration and catalogue access, independent of runners."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api_contract import api_total, expect_object_list
from mobiwork import MobiWorkClient

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / 'config/promotion_bonus.json'


@dataclass(frozen=True)
class PromotionBonusConfig:
    enabled: bool
    name: str
    folder: str
    filename: str
    catalog_url: str
    report_url: str
    catalog_page_size: int = 200
    # Code prefixes of programmes that accumulate over their whole period (e.g. "Thời gian
    # mua và tham gia tích lũy: 01/04 - 30/09") instead of per calendar month.
    cumulative_programs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all(isinstance(code, str) and code.strip() for code in self.cumulative_programs):
            raise ValueError("cumulative_programs must be non-empty strings")
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
    if "cumulative_programs" in payload:
        payload = {**payload, "cumulative_programs": tuple(payload["cumulative_programs"])}
    return PromotionBonusConfig(**payload)


def program_id(program: dict[str, Any]) -> str:
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
        page_total = api_total(payload, "PromotionBonus catalogue")
        if expected_total is None:
            expected_total = page_total
        elif page_total is not None and page_total != expected_total:
            raise RuntimeError("PromotionBonus catalogue total changed during pagination")

        page_rows = expect_object_list(payload, "data", "PromotionBonus catalogue")
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
        identity = program_id(program)
        if not identity:
            raise ValueError(
                f"PromotionBonus catalogue row {row_number} is missing required _id"
            )
        previous = unique.get(identity)
        if previous is not None and previous != program:
            raise ValueError(
                f"PromotionBonus catalogue has conflicting duplicate _id={identity}"
            )
        unique[identity] = program
    if expected_total is not None and len(unique) != expected_total:
        raise RuntimeError(
            f"PromotionBonus catalogue API total={expected_total}, unique programs={len(unique)}"
        )
    return list(unique.values())
