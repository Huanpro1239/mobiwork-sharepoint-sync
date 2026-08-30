"""Synchronize private AI assets from SharePoint onto a persistent runner cache."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from project_paths import AI_ASSET_ROOT, REFERENCE_DIR, TEMPLATE_EXCEL, WEIGHTS_DIR, ensure_runtime_dirs


@dataclass(frozen=True)
class AssetSyncResult:
    downloaded: int
    skipped: int
    removed: int
    manifest_path: Path


class SharePointAssetManager:
    """Mirror changed model/reference/template assets without putting them in Git."""

    def __init__(self, client: Any, drive_id: str) -> None:
        self.client = client
        self.drive_id = drive_id
        self.remote_root = os.environ.get("AI_SHAREPOINT_ASSET_ROOT", "Model Assets").strip().strip("/")
        self.manifest_path = AI_ASSET_ROOT / ".asset_manifest.json"

    def _load_manifest(self) -> dict[str, dict[str, Any]]:
        if not self.manifest_path.is_file():
            return {}
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_manifest(self, payload: dict[str, dict[str, Any]]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.manifest_path)

    def _sync_file(
        self,
        remote_path: str,
        local_path: Path,
        manifest: dict[str, dict[str, Any]],
    ) -> tuple[int, int]:
        item = self.client.get_item_by_path(self.drive_id, remote_path)
        if not item or "folder" in item:
            raise FileNotFoundError(f"AI asset not found on SharePoint: {remote_path}")
        fingerprint = {
            "etag": item.get("eTag") or item.get("cTag") or "",
            "size": int(item.get("size") or 0),
        }
        if local_path.is_file() and manifest.get(remote_path) == fingerprint:
            return 0, 1
        content = self.client.download_file_bytes(self.drive_id, remote_path)
        if content is None:
            raise FileNotFoundError(f"AI asset disappeared during download: {remote_path}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = local_path.with_name(f".{local_path.name}.download")
        temporary.write_bytes(content)
        temporary.replace(local_path)
        manifest[remote_path] = fingerprint
        return 1, 0

    def _sync_tree(
        self,
        remote_folder: str,
        local_folder: Path,
        manifest: dict[str, dict[str, Any]],
        seen_remote_files: set[str],
    ) -> tuple[int, int]:
        folder = self.client.get_item_by_path(self.drive_id, remote_folder)
        if not folder or "folder" not in folder:
            raise FileNotFoundError(f"AI asset folder not found on SharePoint: {remote_folder}")
        downloaded = skipped = 0
        for item in self.client.list_folder_children(self.drive_id, remote_folder):
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            remote = f"{remote_folder}/{name}"
            local = local_folder / name
            if "folder" in item:
                got, kept = self._sync_tree(remote, local, manifest, seen_remote_files)
            else:
                seen_remote_files.add(remote)
                got, kept = self._sync_file(remote, local, manifest)
            downloaded += got
            skipped += kept
        return downloaded, skipped

    @staticmethod
    def _remove_empty_parents(path: Path, stop: Path) -> None:
        current = path
        while current != stop and current.is_dir():
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def _remove_stale_reference_files(
        self,
        manifest: dict[str, dict[str, Any]],
        seen_remote_files: set[str],
    ) -> int:
        remote_prefix = f"{self.remote_root}/reference/"
        stale = sorted(
            remote
            for remote in tuple(manifest)
            if remote.startswith(remote_prefix) and remote not in seen_remote_files
        )
        removed = 0
        for remote in stale:
            relative = remote.removeprefix(remote_prefix)
            local = REFERENCE_DIR / Path(relative)
            if local.is_file():
                local.unlink()
                removed += 1
                self._remove_empty_parents(local.parent, REFERENCE_DIR)
            manifest.pop(remote, None)
        return removed

    def sync_required_assets(self) -> AssetSyncResult:
        ensure_runtime_dirs()
        manifest = self._load_manifest()
        downloaded = skipped = 0
        seen_reference_files: set[str] = set()
        remote_reference = f"{self.remote_root}/reference"
        got, kept = self._sync_tree(
            remote_reference,
            REFERENCE_DIR,
            manifest,
            seen_reference_files,
        )
        downloaded += got
        skipped += kept
        removed = self._remove_stale_reference_files(manifest, seen_reference_files)

        specs = (
            (f"{self.remote_root}/reference_overrides.csv", REFERENCE_DIR / "reference_overrides.csv"),
            (f"{self.remote_root}/weights/yolov8s-world.pt", WEIGHTS_DIR / "yolov8s-world.pt"),
            (f"{self.remote_root}/template/KPI_template.xlsx", TEMPLATE_EXCEL),
        )
        for remote, local in specs:
            got, kept = self._sync_file(remote, local, manifest)
            downloaded += got
            skipped += kept
        self._save_manifest(manifest)
        return AssetSyncResult(downloaded, skipped, removed, self.manifest_path)
