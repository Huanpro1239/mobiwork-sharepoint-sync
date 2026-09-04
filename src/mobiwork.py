from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from region_mapping import enrich_visit_records


LOG = logging.getLogger("mobiwork_sync")


@dataclass(frozen=True)
class ReportConfig:
    key: str
    enabled: bool
    name: str
    folder: str
    url: str | None = None
    url_env: str | None = None
    method: str = "GET"
    from_param: str | None = None
    to_param: str | None = None
    date_format: str = "%Y-%m-%d"
    page_param: str | None = None
    page_size_param: str | None = None
    page_size: int = 500
    data_path: str | None = "data"
    total_path: str | None = None
    explode_field: str | None = None
    fixed_params: dict[str, Any] = field(default_factory=dict)
    export_mode: str = "flat"
    primary_key: list[str] = field(default_factory=list)
    required_fields: list[str] = field(default_factory=list)
    upsert_keys: list[str] = field(default_factory=list)


def get_by_path(payload: Any, path: str | None) -> Any:
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def expand_records(
    records: list[dict[str, Any]], explode_field: str | None
) -> list[dict[str, Any]]:
    """Flatten one nested list field while carrying parent fields into every child row."""
    if not explode_field:
        return records

    expanded: list[dict[str, Any]] = []
    for parent in records:
        children = parent.get(explode_field, [])
        if children is None:
            children = []
        if not isinstance(children, list):
            raise TypeError(
                f"explode_field={explode_field!r} must contain a list, "
                f"got {type(children).__name__}"
            )

        parent_fields = {key: value for key, value in parent.items() if key != explode_field}
        for child in children:
            if not isinstance(child, dict):
                raise TypeError(
                    f"explode_field={explode_field!r} contains a non-object item: "
                    f"{type(child).__name__}"
                )
            expanded.append({**parent_fields, **child})

    return expanded


def validate_records(records: list[dict[str, Any]], cfg: ReportConfig) -> None:
    """Fail fast when a report violates configured business-key expectations."""
    if cfg.required_fields:
        for row_number, row in enumerate(records, start=1):
            missing = [
                field_name
                for field_name in cfg.required_fields
                if field_name not in row or row.get(field_name) in (None, "")
            ]
            if missing:
                raise ValueError(
                    f"Report {cfg.key}: row {row_number} is missing required fields: "
                    f"{', '.join(missing)}"
                )

    if not cfg.primary_key:
        return

    seen: set[tuple[Any, ...]] = set()
    for row_number, row in enumerate(records, start=1):
        key = tuple(row.get(field_name) for field_name in cfg.primary_key)
        if any(value in (None, "") for value in key):
            raise ValueError(
                f"Report {cfg.key}: row {row_number} has an empty primary key "
                f"{cfg.primary_key}"
            )
        if key in seen:
            raise ValueError(
                f"Report {cfg.key}: duplicate primary key {cfg.primary_key}={key}"
            )
        seen.add(key)


def _page_signature(records: list[dict[str, Any]]) -> str:
    """Return a deterministic signature used to detect APIs that repeat a page forever."""
    return json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


def _deduplicate_exact_primary_keys(
    records: list[dict[str, Any]],
    cfg: ReportConfig,
) -> list[dict[str, Any]]:
    """Drop only byte-equivalent business-key duplicates caused by page overlap.

    MobiWork pagination can overlap boundary records when data changes while pages are
    being fetched. An exact duplicate is safe to collapse. Two different payloads with
    the same configured primary key are *not* guessed at: they remain a hard error so
    the pipeline never silently chooses the wrong document/customer version.
    """
    if not cfg.primary_key or not records:
        return records

    seen: dict[tuple[Any, ...], str] = {}
    unique: list[dict[str, Any]] = []
    dropped = 0

    for row in records:
        key = tuple(row.get(field_name) for field_name in cfg.primary_key)
        if any(value in (None, "") for value in key):
            unique.append(row)
            continue

        fingerprint = _page_signature([row])
        previous = seen.get(key)
        if previous is None:
            seen[key] = fingerprint
            unique.append(row)
            continue
        if previous == fingerprint:
            dropped += 1
            continue

        raise ValueError(
            f"Report {cfg.key}: conflicting duplicate primary key "
            f"{cfg.primary_key}={key}; refusing to guess which payload is authoritative"
        )

    if dropped:
        LOG.warning(
            "Report %s: collapsed %s exact duplicate row(s) caused by API/page overlap",
            cfg.key,
            dropped,
        )
    return unique


class MobiWorkClient:
    def __init__(
        self,
        user: str,
        token: str,
        timeout: int = 120,
        min_interval_seconds: float = 1.5,
        max_retries: int = 8,
        session: requests.Session | None = None,
    ) -> None:
        if not user or not token:
            raise ValueError("Missing MOBIWORK_USER or MOBIWORK_TOKEN")
        if timeout < 1:
            raise ValueError("timeout must be >= 1")
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")
        if max_retries < 0 or max_retries > 20:
            raise ValueError("max_retries must be between 0 and 20")

        self.timeout = timeout
        self.min_interval_seconds = min_interval_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self.session = session or requests.Session()
        self.session.auth = HTTPBasicAuth(user, token)
        self.session.headers.update({"Accept": "application/json"})

    @classmethod
    def from_env(cls) -> "MobiWorkClient":
        return cls(
            user=os.environ.get("MOBIWORK_USER", ""),
            token=os.environ.get("MOBIWORK_TOKEN", ""),
            timeout=int(os.environ.get("MOBIWORK_TIMEOUT_SECONDS", "120")),
            min_interval_seconds=float(
                os.environ.get("MOBIWORK_MIN_INTERVAL_SECONDS", "1.5")
            ),
            max_retries=int(os.environ.get("MOBIWORK_MAX_RETRIES", "8")),
        )

    def _throttle(self) -> None:
        if self.min_interval_seconds <= 0 or self._last_request_at <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.min_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    @staticmethod
    def _retry_delay(response: requests.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = str(response.headers.get("Retry-After", "")).strip()
            if retry_after:
                try:
                    return min(max(float(retry_after), 1.0), 180.0)
                except ValueError:
                    pass

        base = min(5.0 * (2**attempt), 60.0)
        return base + random.uniform(0.0, min(base * 0.1, 3.0))

    def _get_with_retry(
        self, url: str, params: dict[str, Any], report_key: str, page: int
    ) -> requests.Response:
        retryable_statuses = {429, 500, 502, 503, 504}

        for attempt in range(self.max_retries + 1):
            self._throttle()
            response: requests.Response | None = None
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                self._last_request_at = time.monotonic()

                if response.status_code not in retryable_statuses:
                    response.raise_for_status()
                    return response

                if attempt >= self.max_retries:
                    response.raise_for_status()
            except (requests.Timeout, requests.ConnectionError) as exc:
                self._last_request_at = time.monotonic()
                if attempt >= self.max_retries:
                    raise
                delay = self._retry_delay(None, attempt)
                LOG.warning(
                    "MobiWork network error report=%s page=%s: %s. "
                    "Retry %s/%s in %.1fs",
                    report_key,
                    page,
                    type(exc).__name__,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                time.sleep(delay)
                continue

            delay = self._retry_delay(response, attempt)
            LOG.warning(
                "MobiWork HTTP %s report=%s page=%s. Retry %s/%s in %.1fs",
                response.status_code if response is not None else "?",
                report_key,
                page,
                attempt + 1,
                self.max_retries,
                delay,
            )
            time.sleep(delay)

        raise RuntimeError("Unreachable retry loop")

    def fetch_report(self, cfg: ReportConfig, target_date: date) -> list[dict[str, Any]]:
        return self.fetch_report_range(cfg, target_date, target_date)

    def fetch_report_range(
        self, cfg: ReportConfig, from_date: date, to_date: date
    ) -> list[dict[str, Any]]:
        if to_date < from_date:
            raise ValueError("to_date must be on or after from_date")

        url = (cfg.url or "").strip()
        if not url and cfg.url_env:
            url = os.environ.get(cfg.url_env, "").strip()
        if not url:
            raise ValueError(
                f"Missing endpoint for report={cfg.key}; configure url or url_env"
            )

        base_params: dict[str, Any] = dict(cfg.fixed_params)
        if cfg.from_param:
            base_params[cfg.from_param] = from_date.strftime(cfg.date_format)
        if cfg.to_param:
            base_params[cfg.to_param] = to_date.strftime(cfg.date_format)

        all_records: list[dict[str, Any]] = []
        raw_record_count = 0
        expected_total: int | None = None
        seen_page_signatures: set[str] = set()
        page = 1

        while True:
            params = dict(base_params)
            if cfg.page_param:
                params[cfg.page_param] = page
            if cfg.page_size_param:
                params[cfg.page_size_param] = cfg.page_size

            if cfg.method.upper() != "GET":
                raise NotImplementedError(f"Unsupported method: {cfg.method}")

            response = self._get_with_retry(url, params, cfg.key, page)
            payload = response.json()

            if isinstance(payload, dict) and payload.get("status") is False:
                raise RuntimeError(
                    f"MobiWork report={cfg.key} returned status=false: "
                    f"{payload.get('message', '')}"
                )

            if cfg.total_path and expected_total is None:
                total_value = get_by_path(payload, cfg.total_path)
                if total_value not in (None, ""):
                    try:
                        expected_total = int(total_value)
                    except (TypeError, ValueError) as exc:
                        raise TypeError(
                            f"Report {cfg.key}: total_path={cfg.total_path!r} "
                            f"is not an integer: {total_value!r}"
                        ) from exc

            records = get_by_path(payload, cfg.data_path)
            if records is None:
                raise ValueError(
                    f"Report {cfg.key}: data_path={cfg.data_path!r} not found in response"
                )
            if isinstance(records, dict):
                records = [records]
            if not isinstance(records, list):
                raise TypeError(
                    f"Report {cfg.key}: expected list, got {type(records).__name__}"
                )

            invalid_types = [
                type(row).__name__ for row in records if not isinstance(row, dict)
            ]
            if invalid_types:
                raise TypeError(
                    f"Report {cfg.key}: response data contains non-object rows: "
                    f"{', '.join(sorted(set(invalid_types)))}"
                )

            clean_records: list[dict[str, Any]] = records
            if cfg.page_param and clean_records:
                signature = _page_signature(clean_records)
                if signature in seen_page_signatures:
                    raise RuntimeError(
                        f"Report {cfg.key}: API repeated page {page}; refusing to "
                        "continue because pagination may be stuck or incomplete"
                    )
                seen_page_signatures.add(signature)

            raw_record_count += len(clean_records)
            all_records.extend(expand_records(clean_records, cfg.explode_field))

            if not cfg.page_param:
                break
            if expected_total is not None and raw_record_count >= expected_total:
                break
            # Do not infer EOF from a short page. Some APIs return fewer rows than
            # the requested page_size while additional pages still exist. Without a
            # source total, only an explicit empty page safely confirms completion.
            if not clean_records:
                break

            page += 1
            if page > 10_000:
                raise RuntimeError(f"Report {cfg.key}: pagination safety limit exceeded")

        if expected_total is not None and raw_record_count != expected_total:
            raise RuntimeError(
                f"Report {cfg.key}: API total={expected_total}, fetched={raw_record_count}. "
                "Refusing to export an incomplete dataset."
            )

        if cfg.key == "visit":
            all_records = enrich_visit_records(all_records)
        all_records = _deduplicate_exact_primary_keys(all_records, cfg)
        validate_records(all_records, cfg)
        return all_records
