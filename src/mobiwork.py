from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests
from requests.auth import HTTPBasicAuth


@dataclass(frozen=True)
class ReportConfig:
    key: str
    enabled: bool
    name: str
    folder: str
    url_env: str
    method: str = "GET"
    from_param: str | None = None
    to_param: str | None = None
    date_format: str = "%Y-%m-%d"
    page_param: str | None = None
    page_size_param: str | None = None
    page_size: int = 500
    data_path: str | None = "data"


def get_by_path(payload: Any, path: str | None) -> Any:
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


class MobiWorkClient:
    def __init__(self, user: str, token: str, timeout: int = 120) -> None:
        if not user or not token:
            raise ValueError("Missing MOBIWORK_USER or MOBIWORK_TOKEN")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(user, token)
        self.session.headers.update({"Accept": "application/json"})

    @classmethod
    def from_env(cls) -> "MobiWorkClient":
        return cls(
            user=os.environ.get("MOBIWORK_USER", ""),
            token=os.environ.get("MOBIWORK_TOKEN", ""),
        )

    def fetch_report(self, cfg: ReportConfig, target_date: date) -> list[dict[str, Any]]:
        url = os.environ.get(cfg.url_env, "").strip()
        if not url:
            raise ValueError(f"Missing endpoint secret/variable: {cfg.url_env}")

        date_text = target_date.strftime(cfg.date_format)
        base_params: dict[str, Any] = {}
        if cfg.from_param:
            base_params[cfg.from_param] = date_text
        if cfg.to_param:
            base_params[cfg.to_param] = date_text

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

            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            records = get_by_path(payload, cfg.data_path)

            if records is None:
                raise ValueError(
                    f"Report {cfg.key}: data_path={cfg.data_path!r} not found in response"
                )
            if isinstance(records, dict):
                records = [records]
            if not isinstance(records, list):
                raise TypeError(f"Report {cfg.key}: expected list, got {type(records).__name__}")

            clean_records = [row for row in records if isinstance(row, dict)]
            all_records.extend(clean_records)

            if not cfg.page_param:
                break
            if len(records) < cfg.page_size:
                break

            page += 1
            if page > 10_000:
                raise RuntimeError(f"Report {cfg.key}: pagination safety limit exceeded")

        return all_records
