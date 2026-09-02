"""Deterministic unique-URL queue and finite retry state for cloud scoring."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Sequence


LEGACY_AUTO_FINAL_LABELS = frozenset({"bien_hieu", "trung_bay", "khong_dat"})
LEGACY_RESCORE_LABELS = frozenset({"", "can_duyet", "khong_the_cham"})
LEGACY_RESCORE_STATUSES = frozenset(
    {"LEGACY_REVIEW", "TECHNICAL_FAILURE", "PENDING_SCORE"}
)


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def legacy_requires_rescore(row: Mapping[str, object]) -> bool:
    """Return whether a legacy payload must pass through the current model."""

    label = _text(row.get("Phân Loại AI")).casefold()
    status = _text(
        row.get("Trạng Thái Quyết Định") or row.get("Quyết Định")
    ).upper()
    if status.startswith("REVIEW_") or status in LEGACY_RESCORE_STATUSES:
        return True
    if label in LEGACY_RESCORE_LABELS:
        return True
    return label not in LEGACY_AUTO_FINAL_LABELS


def _queue_key(row: Mapping[str, object], index: int) -> tuple[str, str]:
    url = _text(row.get("hinh_anh"))
    if url:
        return url, url
    record_id = _text(row.get("record_id"))
    synthetic = f"record:{record_id or index}"
    return synthetic, ""


def _date_sort_value(value: object) -> tuple[int, str]:
    text = _text(value)
    if not text:
        return 1, ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0, text
    return 0, parsed.isoformat()


@dataclass(frozen=True)
class PendingURLGroup:
    key: str
    url: str
    indices: tuple[int, ...]
    attempts: int
    oldest_date: tuple[int, str]
    newest_date: tuple[int, str]


@dataclass(frozen=True)
class PendingURLSelection:
    selected: tuple[PendingURLGroup, ...]
    deferred: tuple[PendingURLGroup, ...]
    blocked: tuple[PendingURLGroup, ...]


def _groups(
    rows: Sequence[Mapping[str, object]],
    candidate_indices: Iterable[int],
    attempts_by_url: Mapping[str, int],
) -> list[PendingURLGroup]:
    grouped: dict[str, dict[str, object]] = {}
    for index in candidate_indices:
        row = rows[index]
        key, url = _queue_key(row, index)
        date = _date_sort_value(row.get("ngay"))
        group = grouped.setdefault(
            key,
            {"url": url, "indices": [], "dates": []},
        )
        group["indices"].append(index)  # type: ignore[union-attr]
        group["dates"].append(date)  # type: ignore[union-attr]

    result: list[PendingURLGroup] = []
    for key, group in grouped.items():
        dates = group["dates"]
        try:
            attempts = max(0, int(attempts_by_url.get(key, 0)))
        except (TypeError, ValueError):
            attempts = 0
        result.append(
            PendingURLGroup(
                key=key,
                url=str(group["url"]),
                indices=tuple(group["indices"]),
                attempts=attempts,
                oldest_date=min(dates),
                newest_date=max(dates),
            )
        )
    return result


def select_pending_url_groups(
    rows: Sequence[Mapping[str, object]],
    *,
    candidate_indices: Iterable[int],
    attempts_by_url: Mapping[str, int],
    limit: int,
    max_attempts: int,
    selection: str,
) -> PendingURLSelection:
    """Select a bounded unique-URL batch, prioritizing never-attempted work."""

    if limit < 0:
        raise ValueError("limit must be >= 0")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if selection not in {"oldest", "latest"}:
        raise ValueError("selection must be 'oldest' or 'latest'")

    all_groups = _groups(rows, candidate_indices, attempts_by_url)
    blocked = [group for group in all_groups if group.attempts >= max_attempts]
    eligible = [group for group in all_groups if group.attempts < max_attempts]

    if selection == "oldest":
        eligible.sort(key=lambda group: (group.oldest_date, group.key))
    else:
        eligible.sort(key=lambda group: (group.newest_date, group.key), reverse=True)
    eligible.sort(key=lambda group: group.attempts > 0)
    blocked.sort(key=lambda group: (group.oldest_date, group.key))

    selected_count = len(eligible) if limit == 0 else min(limit, len(eligible))
    return PendingURLSelection(
        selected=tuple(eligible[:selected_count]),
        deferred=tuple(eligible[selected_count:]),
        blocked=tuple(blocked),
    )


@dataclass(frozen=True)
class RetryUpdate:
    attempts_by_url: dict[str, int]
    retryable_urls: frozenset[str]
    blocked_urls: frozenset[str]


def advance_retry_attempts(
    *,
    attempts_by_url: Mapping[str, int],
    succeeded_urls: Iterable[str],
    failed_urls: Iterable[str],
    max_attempts: int,
) -> RetryUpdate:
    """Advance persistent failure counts and remove URLs that succeeded."""

    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    attempts: dict[str, int] = {}
    for key, value in attempts_by_url.items():
        try:
            count = max(0, int(value))
        except (TypeError, ValueError):
            continue
        if count:
            attempts[str(key)] = count

    for key in succeeded_urls:
        attempts.pop(str(key), None)
    for key in failed_urls:
        normalized = str(key)
        attempts[normalized] = attempts.get(normalized, 0) + 1

    blocked = frozenset(
        key for key, count in attempts.items() if count >= max_attempts
    )
    retryable = frozenset(
        key for key, count in attempts.items() if 0 < count < max_attempts
    )
    return RetryUpdate(attempts, retryable, blocked)
