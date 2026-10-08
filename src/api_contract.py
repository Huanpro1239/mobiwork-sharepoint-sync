"""Shared validation of documented OpenAPI payload shapes."""
from __future__ import annotations

from typing import Any


def expect_object_list(payload: dict[str, Any], key: str, operation: str) -> list[dict[str, Any]]:
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


def optional_object_list(
    payload: dict[str, Any],
    key: str,
    operation: str,
) -> list[dict[str, Any]]:
    if payload.get(key) is None:
        return []
    return expect_object_list(payload, key, operation)


def api_total(payload: dict[str, Any], operation: str) -> int | None:
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
