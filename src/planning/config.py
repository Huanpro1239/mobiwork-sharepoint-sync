from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceRef:
    key: str
    path: str
    sheet: str | None = None
    start_row: int | None = None


@dataclass(frozen=True)
class PlanningConfig:
    planning_master_path: str
    shadow_output_folder: str
    sources: dict[str, SourceRef]

    @classmethod
    def load(cls, path: Path) -> "PlanningConfig":
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        sources = {
            key: SourceRef(key=key, **value)
            for key, value in payload.get("sources", {}).items()
        }
        return cls(
            planning_master_path=str(payload["planning_master_path"]),
            shadow_output_folder=str(payload.get("shadow_output_folder", "_PlanningEngine/shadow")),
            sources=sources,
        )
