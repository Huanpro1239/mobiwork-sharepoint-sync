"""Common promotion result contract for calculated and DMS web sources."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

FINAL, EMPTY, ENVELOPE = 'final', 'empty', 'envelope'


@dataclass
class ProgramResult:
    program: dict[str, Any]
    status: str
    attempts: int
    rows: list[dict[str, Any]] = field(default_factory=list)
    targets: list[dict[str, Any]] = field(default_factory=list)
    rewards: list[dict[str, Any]] = field(default_factory=list)

    @property
    def program_id(self) -> str:
        return str(self.program.get("_id", ""))

    @property
    def program_name(self) -> str:
        return str(self.program.get("name", "")).strip()


def text(value: Any) -> str:
    if value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, dict):
        for key in ("viewData", "name", "ten", "label", "value"):
            if value.get(key) not in (None, ""):
                return text(value[key])
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (text(v) for v in value) if part)
    return str(value).strip()
