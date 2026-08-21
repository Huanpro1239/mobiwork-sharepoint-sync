from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import requests
from requests.auth import HTTPBasicAuth


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
    explode_field: str | None = None
    fixed_params: dict[str, Any] = field(default_factory=dict)
    export_mode: str = "flat"


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
                continue
            expanded.append({**parent_fields, **child})

    return expanded


class MobiWorkClient:
    def __init__(
        self,
        user: str,
        token: str,
        timeout: int = 120,
        min_interval_seconds: float = 1.5,
        max_retries: int = 8,
    ) -> None:
        if not user or not token:
            raise ValueError("Missing MOBIWORK_USER or MOBIWORK_TOKEN")
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")
        if max_retries < 0 or max_retries > 20:
            raise ValueError("max_retries must be between 0 and 20")

        self.timeout = timeout
        self.min_interval_seconds = min_interval_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(user, token)
        self.session.headers.update({"Accept": "application/json"})

    @classmethod
    def from_env(cls) -> "MobiWorkClient":
        return cls(
            user=os.environ.get("MOBIWORK_USER", ""),
            token=os.environ.get("MOBIWORK_TOKEN", ""),
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
    def _retry_delay(response: requests.Response, attempt: int) -> float:
        retry_after = str(response.headers.get("Retry-After", "")).strip()
        if retry_after:
            try:
                return min(max(float(retry_after), 1.0), 180.0)
            except ValueError:
                pass

        # 5, 10, 20, 40, 60, 60... seconds.
        return min(5.0 * (2**attempt), 60.0)

    def _get_with_retry(
        self, url: str, params: dict[str, Any], report_key: str, page: int
    ) -> requests.Response:
        retryable_statuses = {429, 500, 502, 503, 504}

        for attempt in range(self.max_retries + 1):
            self._throttle()
            response = self.session.get(url, params=params, timeout=self.timeout)
            self._last_request_at = time.monotonic()

            if response.status_code not in retryable_statuses:
                response.raise_for_status()
                return response

            if attempt >= self.max_retries:
                response.raise_for_status()

            delay = self._retry_delay(response, attempt)
            LOG.warning(
                "MobiWork HTTP %s report=%s page=%s. Retry %s/%s in %.0fs",
                response.status_code,
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

            clean_records = [row for row in records if isinstance(row, dict)]
            all_records.extend(expand_records(clean_records, cfg.explode_field))

            if not cfg.page_param:
                break
            if len(records) < cfg.page_size:
                break

            page += 1
            if page > 10_000:
                raise RuntimeError(f"Report {cfg.key}: pagination safety limit exceeded")

        return all_records
