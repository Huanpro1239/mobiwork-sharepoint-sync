from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import requests
from azure.identity import AzureCliCredential


GRAPH = "https://graph.microsoft.com/v1.0"


class SharePointClient:
    def __init__(
        self,
        host: str,
        site_path: str,
        library_name: str,
        timeout: int = 120,
    ) -> None:
        self.host = host.strip()
        self.site_path = "/" + site_path.strip("/")
        self.library_name = library_name.strip()
        self.timeout = timeout
        if not all([self.host, self.site_path, self.library_name]):
            raise ValueError("SharePoint host/site/library configuration is incomplete")

        credential = AzureCliCredential()
        token = credential.get_token("https://graph.microsoft.com/.default").token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    @classmethod
    def from_env(cls) -> "SharePointClient":
        return cls(
            host=os.environ.get("SHAREPOINT_HOST", "vikodacomvn.sharepoint.com"),
            site_path=os.environ.get("SHAREPOINT_SITE_PATH", "/sites/Planning"),
            library_name=os.environ.get("SHAREPOINT_LIBRARY", "MobiWorkDMS"),
        )

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        response = requests.request(
            method,
            url,
            headers=kwargs.pop("headers", self.headers),
            timeout=kwargs.pop("timeout", self.timeout),
            **kwargs,
        )
        response.raise_for_status()
        return response

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

    def _get_item_by_path(self, drive_id: str, remote_path: str) -> dict | None:
        encoded = quote(remote_path.strip("/"), safe="/")
        url = f"{GRAPH}/drives/{drive_id}/root:/{encoded}"
        response = requests.get(url, headers=self.headers, timeout=self.timeout)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def ensure_folder_path(self, drive_id: str, folder_path: str) -> str:
        parent_id = "root"
        built: list[str] = []
        for segment in [part for part in folder_path.strip("/").split("/") if part]:
            built.append(segment)
            existing = self._get_item_by_path(drive_id, "/".join(built))
            if existing:
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
            created = self._request("POST", url, json=payload).json()
            parent_id = created["id"]
        return parent_id

    def upload_file(self, drive_id: str, local_file: Path, remote_folder: str) -> dict:
        local_file = Path(local_file)
        if not local_file.is_file():
            raise FileNotFoundError(local_file)

        parent_id = self.ensure_folder_path(drive_id, remote_folder)
        filename = quote(local_file.name, safe="")
        url = f"{GRAPH}/drives/{drive_id}/items/{parent_id}:/{filename}:/content"
        headers = {
            "Authorization": self.headers["Authorization"],
            "Content-Type": "application/octet-stream",
        }
        with local_file.open("rb") as handle:
            return self._request(
                "PUT",
                url,
                headers=headers,
                data=handle,
                timeout=300,
            ).json()
