from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from azure.identity import AzureCliCredential


GRAPH = "https://graph.microsoft.com/v1.0"
LOG = logging.getLogger("mobiwork_sync")


class SharePointClient:
    def __init__(
        self,
        host: str,
        site_path: str,
        library_name: str,
        timeout: int = 120,
        max_retries: int = 6,
        credential: Any | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.host = host.strip()
        self.site_path = "/" + site_path.strip("/")
        self.library_name = library_name.strip()
        self.timeout = timeout
        self.max_retries = max_retries
        if not all([self.host, self.site_path, self.library_name]):
            raise ValueError("SharePoint host/site/library configuration is incomplete")
        if timeout < 1:
            raise ValueError("timeout must be >= 1")
        if max_retries < 0 or max_retries > 20:
            raise ValueError("max_retries must be between 0 and 20")

        self.credential = credential or AzureCliCredential()
        self.session = session or requests.Session()
        self._token: str | None = None
        self._token_expires_on = 0.0

    @classmethod
    def from_env(cls) -> "SharePointClient":
        return cls(
            host=os.environ.get("SHAREPOINT_HOST", "vikodacomvn.sharepoint.com"),
            site_path=os.environ.get("SHAREPOINT_SITE_PATH", "/sites/Planning"),
            library_name=os.environ.get("SHAREPOINT_LIBRARY", "MobiWorkDMS"),
            timeout=int(os.environ.get("SHAREPOINT_TIMEOUT_SECONDS", "120")),
            max_retries=int(os.environ.get("SHAREPOINT_MAX_RETRIES", "6")),
        )

    def _access_token(self, force_refresh: bool = False) -> str:
        now = time.time()
        if (
            not force_refresh
            and self._token
            and self._token_expires_on > now + 300
        ):
            return self._token

        access_token = self.credential.get_token("https://graph.microsoft.com/.default")
        self._token = access_token.token
        self._token_expires_on = float(getattr(access_token, "expires_on", now + 300))
        return self._token

    def _headers(
        self,
        extra: dict[str, str] | None = None,
        force_refresh: bool = False,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token(force_refresh)}",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _retry_delay(response: requests.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = str(response.headers.get("Retry-After", "")).strip()
            if retry_after:
                try:
                    return min(max(float(retry_after), 1.0), 180.0)
                except ValueError:
                    pass
        base = min(2.0 * (2**attempt), 60.0)
        return base + random.uniform(0.0, min(base * 0.15, 3.0))

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        allow_404: bool = False,
        **kwargs: Any,
    ) -> requests.Response:
        retryable_statuses = {429, 500, 502, 503, 504}
        force_refresh = False

        for attempt in range(self.max_retries + 1):
            response: requests.Response | None = None
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=self._headers(headers, force_refresh=force_refresh),
                    timeout=timeout or self.timeout,
                    **kwargs,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= self.max_retries:
                    raise
                delay = self._retry_delay(None, attempt)
                LOG.warning(
                    "Microsoft Graph network error: %s. Retry %s/%s in %.1fs",
                    type(exc).__name__,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                time.sleep(delay)
                continue

            if allow_404 and response.status_code == 404:
                return response

            if response.status_code == 401 and attempt < self.max_retries:
                LOG.warning("Microsoft Graph token rejected; refreshing credentials and retrying")
                force_refresh = True
                self._token = None
                self._token_expires_on = 0.0
                continue

            force_refresh = False
            if response.status_code not in retryable_statuses:
                response.raise_for_status()
                return response

            if attempt >= self.max_retries:
                response.raise_for_status()

            delay = self._retry_delay(response, attempt)
            LOG.warning(
                "Microsoft Graph HTTP %s. Retry %s/%s in %.1fs",
                response.status_code,
                attempt + 1,
                self.max_retries,
                delay,
            )
            time.sleep(delay)

        raise RuntimeError("Unreachable retry loop")

    def get_site_id(self) -> str:
        url = f"{GRAPH}/sites/{self.host}:{self.site_path}"
        return self._request("GET", url).json()["id"]

    def get_drive_id(self, site_id: str) -> str:
        url = f"{GRAPH}/sites/{site_id}/drives"
        drives = self._request("GET", url).json().get("value", [])
        for drive in drives:
            if str(drive.get("name", "")).casefold() == self.library_name.casefold():
                return drive["id"]
        available = ", ".join(str(d.get("name", "?")) for d in drives)
        raise RuntimeError(
            f"SharePoint library {self.library_name!r} not found. Available drives: {available}"
        )

    def get_item_by_path(self, drive_id: str, remote_path: str) -> dict[str, Any] | None:
        encoded = quote(remote_path.strip("/"), safe="/")
        url = f"{GRAPH}/drives/{drive_id}/root:/{encoded}"
        response = self._request("GET", url, allow_404=True)
        if response.status_code == 404:
            return None
        return response.json()

    def ensure_folder_path(self, drive_id: str, folder_path: str) -> str:
        parent_id = "root"
        built: list[str] = []
        for segment in [part for part in folder_path.strip("/").split("/") if part]:
            built.append(segment)
            current_path = "/".join(built)
            existing = self.get_item_by_path(drive_id, current_path)
            if existing:
                if "folder" not in existing:
                    raise RuntimeError(
                        f"SharePoint path {current_path!r} exists but is not a folder"
                    )
                parent_id = existing["id"]
                continue

            url = (
                f"{GRAPH}/drives/{drive_id}/root/children"
                if parent_id == "root"
                else f"{GRAPH}/drives/{drive_id}/items/{parent_id}/children"
            )
            payload = {
                "name": segment,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "fail",
            }
            try:
                created = self._request("POST", url, json=payload).json()
            except requests.HTTPError as exc:
                # Another concurrent process may have created the folder after our GET.
                if exc.response is None or exc.response.status_code != 409:
                    raise
                created = self.get_item_by_path(drive_id, current_path)
                if not created:
                    raise
            parent_id = created["id"]
        return parent_id

    def _put_content(
        self,
        drive_id: str,
        remote_folder: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        parent_id = self.ensure_folder_path(drive_id, remote_folder)
        encoded_name = quote(filename, safe="")
        url = f"{GRAPH}/drives/{drive_id}/items/{parent_id}:/{encoded_name}:/content"
        uploaded = self._request(
            "PUT",
            url,
            headers={"Content-Type": content_type},
            data=content,
            timeout=300,
        ).json()

        remote_size = uploaded.get("size")
        if remote_size is not None and int(remote_size) != len(content):
            raise RuntimeError(
                f"SharePoint upload size mismatch for {filename}: "
                f"local={len(content)}, remote={remote_size}"
            )
        return uploaded

    def upload_file(self, drive_id: str, local_file: Path, remote_folder: str) -> dict[str, Any]:
        local_file = Path(local_file)
        if not local_file.is_file():
            raise FileNotFoundError(local_file)

        # Graph simple upload supports files up to 250 MB. Reading bytes also makes retries safe.
        content = local_file.read_bytes()
        if len(content) > 250 * 1024 * 1024:
            raise ValueError(
                f"{local_file.name} exceeds 250 MB; an upload session is required"
            )
        return self._put_content(
            drive_id,
            remote_folder,
            local_file.name,
            content,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def upload_json(
        self,
        drive_id: str,
        remote_path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        path = remote_path.strip("/")
        folder, _, filename = path.rpartition("/")
        if not filename:
            raise ValueError("remote_path must include a file name")
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        return self._put_content(
            drive_id,
            folder,
            filename,
            content,
            "application/json; charset=utf-8",
        )

    def download_json(self, drive_id: str, remote_path: str) -> dict[str, Any] | None:
        encoded = quote(remote_path.strip("/"), safe="/")
        metadata_url = f"{GRAPH}/drives/{drive_id}/root:/{encoded}"
        metadata = self._request("GET", metadata_url, allow_404=True)
        if metadata.status_code == 404:
            return None

        content_url = f"{GRAPH}/drives/{drive_id}/root:/{encoded}:/content"
        response = self._request("GET", content_url)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(f"SharePoint JSON file {remote_path!r} is invalid") from exc
        if not isinstance(payload, dict):
            raise TypeError(f"SharePoint JSON file {remote_path!r} must contain an object")
        return payload
