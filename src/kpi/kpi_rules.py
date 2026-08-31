"""Business rules for the rolling two-month Sales KPI calculation.

The image model and KPI policy are intentionally separated.  Image labels can be
manually overridden in Excel; the workbook formulas then recalculate customer
eligibility, workdays and rewards without rerunning the AI pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

KPI_RULES_VERSION = "2.4.0"


@dataclass(frozen=True)
class KPIPolicy:
    benchmark_customers: int = 50
    khtc_single_order_ktb: float = 3.0
    khddk_total_ktb: float = 5.0
    new_customer_reward_vnd: int = 30_000
    old_customer_reward_vnd: int = 10_000
    reward_customer_cap: int = 50
    reward_total_cap_vnd: int = 4_000_000
    ktb_units: tuple[str, ...] = ("ket", "thung", "binh")


DEFAULT_KPI_POLICY = KPIPolicy()

# A non-empty note can substitute for signboard evidence only when it does not
# explicitly state that the outlet has no signboard.  This keeps obvious
# negative notes such as "Không biển bảng" from accidentally granting a pass.
NEGATIVE_SIGN_NOTE_MARKERS = (
    "khong bien",
    "khong co bien",
    "khong bang",
    "khong co bang",
    "khong bien bang",
    "khong co bien bang",
    "khong bien hieu",
    "khong co bien hieu",
)

PLACEHOLDER_NOTES = {"", "-", "--", "na", "n/a", "none", "null", "nan"}
ORDER_ID_PATTERN = re.compile(r"\[([^\[\]]+)\]")


def ascii_key(value: object) -> str:
    """Normalize text for joins and conservative keyword rules."""

    text = "" if value is None else str(value)
    text = " ".join(text.strip().casefold().split())
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def is_truthy(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = ascii_key(value)
    if text in {"1", "true", "yes", "y", "x", "co", "có"}:
        return True
    try:
        return float(text) != 0.0
    except (TypeError, ValueError):
        return False


def is_valid_sign_note(value: object) -> bool:
    """Return whether a visit note may substitute for signboard evidence."""

    text = ascii_key(value)
    if text in PLACEHOLDER_NOTES:
        return False
    return not any(marker in text for marker in NEGATIVE_SIGN_NOTE_MARKERS)


def extract_order_id(description: object) -> str:
    text = "" if description is None else str(description).strip()
    match = ORDER_ID_PATTERN.search(text)
    return match.group(1).strip() if match else ""
