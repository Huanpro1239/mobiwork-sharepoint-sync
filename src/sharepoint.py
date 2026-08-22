from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

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
                if exc.response is None or exc.response.status_code != 409:
                    raise
                created = self.get_item_by_path(drive_id, current_path)
                if not created:
                    raise
            parent_id = created["id"]
        return parent_id

    @staticmethod
    def _item_url(drive_id: str, item_id: str) -> str:
        return f"{GRAPH}/drives/{drive_id}/items/{quote(item_id, safe='')}"

    def _download_item_content(self, drive_id: str, item_id: str) -> bytes:
        return self._request(
            "GET",
            f"{self._item_url(drive_id, item_id)}/content",
            timeout=300,
        ).content

    def _wait_for_item_by_path(
        self,
        drive_id: str,
        remote_path: str,
        attempts: int = 4,
    ) -> dict[str, Any] | None:
        for attempt in range(attempts):
            item = self.get_item_by_path(drive_id, remote_path)
            if item:
                return item
            if attempt < attempts - 1:
                time.sleep(min(1.0 * (2**attempt), 3.0))
        return None

    def _verify_uploaded_size(
        self,
        drive_id: str,
        filename: str,
        uploaded: dict[str, Any],
        expected_size: int,
        verification_attempts: int = 3,
        expected_content: bytes | None = None,
    ) -> dict[str, Any]:
        remote_size = uploaded.get("size")
        if remote_size is not None and int(remote_size) == expected_size:
            return uploaded

        item_id = str(uploaded.get("id", "")).strip()
        if not item_id:
            raise RuntimeError(
                f"SharePoint upload size mismatch for {filename}: "
                f"local={expected_size}, remote={remote_size}, item_id=missing"
            )

        last_size = remote_size
        last_metadata: dict[str, Any] = uploaded
        metadata_url = self._item_url(drive_id, item_id)
        for attempt in range(verification_attempts):
            metadata = self._request("GET", metadata_url).json()
            last_metadata = metadata
            last_size = metadata.get("size")
            if last_size is not None and int(last_size) == expected_size:
                return {**uploaded, **metadata}

            if attempt < verification_attempts - 1:
                delay = min(1.0 * (2**attempt), 3.0)
                LOG.warning(
                    "SharePoint metadata size not settled for %s: local=%s, remote=%s. "
                    "Recheck %s/%s in %.1fs",
                    filename,
                    expected_size,
                    last_size,
                    attempt + 1,
                    verification_attempts,
                    delay,
                )
                time.sleep(delay)

        if expected_content is not None:
            actual_content = self._download_item_content(drive_id, item_id)
            if actual_content == expected_content:
                LOG.warning(
                    "SharePoint size metadata remained stale for %s, but downloaded "
                    "content matched the uploaded bytes exactly",
                    filename,
                )
                verified = {**uploaded, **last_metadata}
                verified["size"] = expected_size
                return verified
            raise RuntimeError(
                f"SharePoint upload content mismatch for {filename}: "
                f"local={expected_size}, downloaded={len(actual_content)}, "
                f"metadata={last_size}"
            )

        raise RuntimeError(
            f"SharePoint upload size mismatch for {filename}: "
            f"local={expected_size}, remote={last_size} after metadata recheck"
        )

    def _upload_new_content(
        self,
        drive_id: str,
        remote_folder: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        remote_path = "/".join(
            part for part in (remote_folder.strip("/"), filename) if part
        )
        encoded_path = quote(remote_path, safe="/")
        url = f"{GRAPH}/drives/{drive_id}/root:/{encoded_path}:/content"
        response = self._request(
            "PUT",
            url,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(content)),
            },
            data=content,
            timeout=300,
        )
        uploaded = response.json()
        LOG.info(
            "Graph PUT result status=%s requested=%s returned_name=%s returned_id=%s returned_size=%s",
            response.status_code,
            remote_path,
            uploaded.get("name"),
            uploaded.get("id"),
            uploaded.get("size"),
        )

        returned_name = str(uploaded.get("name", "")).strip()
        if returned_name and returned_name != filename:
            raise RuntimeError(
                f"SharePoint created unexpected item for {remote_path!r}: "
                f"returned_name={returned_name!r}"
            )

        materialized = self._wait_for_item_by_path(drive_id, remote_path)
        if not materialized:
            raise RuntimeError(
                f"SharePoint upload response succeeded but path {remote_path!r} was not materialized"
            )

        response_id = str(uploaded.get("id", "")).strip()
        materialized_id = str(materialized.get("id", "")).strip()
        if response_id and materialized_id and response_id != materialized_id:
            raise RuntimeError(
                f"SharePoint upload item mismatch for {remote_path!r}: "
                f"response_id={response_id}, path_id={materialized_id}"
            )

        verified_source = {**uploaded, **materialized}
        return self._verify_uploaded_size(
            drive_id,
            filename,
            verified_source,
            len(content),
            expected_content=content,
        )

    def _rename_item(
        self,
        drive_id: str,
        item_id: str,
        new_name: str,
        etag: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if etag:
            headers["If-Match"] = etag
        return self._request(
            "PATCH",
            self._item_url(drive_id, item_id),
            headers=headers,
            json={"name": new_name},
        ).json()

    def _delete_item(self, drive_id: str, item_id: str) -> None:
        self._request("DELETE", self._item_url(drive_id, item_id))

    def _verify_exact_item_content(
        self,
        drive_id: str,
        item_id: str,
        filename: str,
        expected_content: bytes,
    ) -> dict[str, Any]:
        metadata = self._request("GET", self._item_url(drive_id, item_id)).json()
        actual_content = self._download_item_content(drive_id, item_id)
        if actual_content != expected_content:
            raise RuntimeError(
                f"SharePoint promoted file content mismatch for {filename}: "
                f"local={len(expected_content)}, downloaded={len(actual_content)}"
            )
        metadata["size"] = len(expected_content)
        return metadata

    def _staged_replace_content(
        self,
        drive_id: str,
        remote_folder: str,
        filename: str,
        content: bytes,
        content_type: str,
        existing: dict[str, Any],
    ) -> dict[str, Any]:
        existing_id = str(existing.get("id", "")).strip()
        if not existing_id:
            raise RuntimeError(f"Existing SharePoint file {filename!r} has no driveItem id")

        self.ensure_folder_path(drive_id, remote_folder)
        token = uuid4().hex[:12]
        temp_name = f"__sync_tmp_{token}__{filename}"
        backup_name = f"__sync_backup_{token}__{filename}"
        failed_name = f"__sync_failed_{token}__{filename}"

        LOG.info("Staging SharePoint replacement: %s -> %s", filename, temp_name)
        temp = self._upload_new_content(
            drive_id,
            remote_folder,
            temp_name,
            content,
            content_type,
        )
        temp_id = str(temp.get("id", "")).strip()
        if not temp_id:
            raise RuntimeError(f"Staged SharePoint file {temp_name!r} has no driveItem id")

        backup_renamed = False
        promoted = False
        try:
            self._rename_item(
                drive_id,
                existing_id,
                backup_name,
                str(existing.get("eTag", "")).strip() or None,
            )
            backup_renamed = True
            LOG.info("Renamed old SharePoint file to backup: %s", backup_name)

            promoted_metadata = self._rename_item(drive_id, temp_id, filename)
            promoted = True
            LOG.info("Promoted staged SharePoint file to canonical name: %s", filename)

            verified = self._verify_exact_item_content(
                drive_id,
                temp_id,
                filename,
                content,
            )
            result = {**temp, **promoted_metadata, **verified}

            try:
                self._delete_item(drive_id, existing_id)
                LOG.info("Removed SharePoint backup after verified promotion: %s", backup_name)
            except Exception:
                LOG.exception("Unable to remove SharePoint backup %s", backup_name)
            return result
        except Exception:
            LOG.exception("SharePoint staged replacement failed for %s; attempting rollback", filename)
            if promoted:
                try:
                    self._rename_item(drive_id, temp_id, failed_name)
                except Exception:
                    LOG.exception("Unable to move failed promoted item away from %s", filename)
            if backup_renamed:
                try:
                    self._rename_item(drive_id, existing_id, filename)
                    LOG.info("Restored previous SharePoint file after failed replacement: %s", filename)
                except Exception:
                    LOG.exception("CRITICAL: unable to restore SharePoint backup for %s", filename)
            if not promoted:
                try:
                    self._delete_item(drive_id, temp_id)
                except Exception:
                    LOG.exception("Unable to remove staged SharePoint temp file %s", temp_name)
            raise

    def _put_content(
        self,
        drive_id: str,
        remote_folder: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        remote_path = "/".join(
            part for part in (remote_folder.strip("/"), filename) if part
        )
        existing = self.get_item_by_path(drive_id, remote_path)
        if existing:
            if "folder" in existing:
                raise RuntimeError(
                    f"SharePoint target {remote_path!r} exists but is a folder"
                )
            return self._staged_replace_content(
                drive_id,
                remote_folder,
                filename,
                content,
                content_type,
                existing,
            )

        self.ensure_folder_path(drive_id, remote_folder)
        LOG.info("Creating new SharePoint file: %s", remote_path)
        return self._upload_new_content(
            drive_id,
            remote_folder,
            filename,
            content,
            content_type,
        )

    def upload_file(self, drive_id: str, local_file: Path, remote_folder: str) -> dict[str, Any]:
        local_file = Path(local_file)
        if not local_file.is_file():
            raise FileNotFoundError(local_file)

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

    def download_file_bytes(self, drive_id: str, remote_path: str) -> bytes | None:
        item = self.get_item_by_path(drive_id, remote_path)
        if not item:
            return None
        if "folder" in item:
            raise RuntimeError(f"SharePoint path {remote_path!r} is a folder, not a file")
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            raise RuntimeError(f"SharePoint file {remote_path!r} has no driveItem id")
        return self._download_item_content(drive_id, item_id)

    def list_folder_children(self, drive_id: str, remote_folder: str) -> list[dict[str, Any]]:
        folder = self.get_item_by_path(drive_id, remote_folder)
        if not folder:
            return []
        if "folder" not in folder:
            raise RuntimeError(f"SharePoint path {remote_folder!r} is not a folder")
        folder_id = str(folder.get("id", "")).strip()
        if not folder_id:
            raise RuntimeError(f"SharePoint folder {remote_folder!r} has no driveItem id")

        url: str | None = f"{self._item_url(drive_id, folder_id)}/children?$top=200"
        items: list[dict[str, Any]] = []
        while url:
            payload = self._request("GET", url).json()
            value = payload.get("value", [])
            if not isinstance(value, list):
                raise TypeError("Microsoft Graph children response value must be a list")
            items.extend(item for item in value if isinstance(item, dict))
            next_link = payload.get("@odata.nextLink")
            url = str(next_link).strip() if next_link else None
        return items

    def delete_path(self, drive_id: str, remote_path: str) -> bool:
        item = self.get_item_by_path(drive_id, remote_path)
        if not item:
            return False
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            raise RuntimeError(f"SharePoint item {remote_path!r} has no driveItem id")
        self._delete_item(drive_id, item_id)
        return True

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
