"""Bootstrap an ephemeral cloud runner from SharePoint-hosted AI assets."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from image_storage import ImageSharePointClient
from project_paths import REFERENCE_DIR, TEMPLATE_EXCEL, ensure_runtime_dirs
from scoring.config import CACHE_FILE, REFERENCE_OVERRIDES, YOLO_WEIGHTS


@dataclass(frozen=True)
class CloudAssetResult:
    downloaded: int
    root: str
    asset_drive_id: str


def _download_file(
    client: ImageSharePointClient,
    drive_id: str,
    remote_path: str,
    local_path: Path,
) -> None:
    content = client.download_file_bytes(drive_id, remote_path)
    if not content:
        raise FileNotFoundError(f"SharePoint AI asset missing or empty: {remote_path}")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = local_path.with_name(f".{local_path.name}.download")
    temporary.write_bytes(content)
    temporary.replace(local_path)


def sync_cloud_assets(client: ImageSharePointClient) -> CloudAssetResult:
    """Download the minimal immutable runtime bundle from SharePoint."""

    ensure_runtime_dirs()
    asset_drive_id = os.environ.get("AI_ASSET_DRIVE_ID", "").strip()
    if not asset_drive_id:
        raise RuntimeError("AI_ASSET_DRIVE_ID is required for cloud runtime")
    root = (
        os.environ.get(
            "AI_SHAREPOINT_ASSET_ROOT",
            "Chạy chương trình/KPI Assets",
        )
        .strip()
        .strip("/")
    )
    if not root:
        raise ValueError("AI_SHAREPOINT_ASSET_ROOT must not be empty")

    specs = (
        (f"{root}/reference_bundle_v2.pkl", CACHE_FILE),
        (f"{root}/yolov8s-world.pt", YOLO_WEIGHTS),
        (f"{root}/KPI_template.xlsx", TEMPLATE_EXCEL),
    )
    for remote_path, local_path in specs:
        _download_file(client, asset_drive_id, remote_path, local_path)

    # Existing validation code expects these reference placeholders. The
    # prebuilt classifier never reads them; it consumes CACHE_FILE directly.
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    if not REFERENCE_OVERRIDES.exists():
        REFERENCE_OVERRIDES.write_text(
            "relative_path,action,new_subcategory\n",
            encoding="utf-8-sig",
        )

    if not CACHE_FILE.is_file() or not YOLO_WEIGHTS.is_file() or not TEMPLATE_EXCEL.is_file():
        raise RuntimeError("Cloud AI asset bootstrap did not materialize all required files")
    return CloudAssetResult(downloaded=len(specs), root=root, asset_drive_id=asset_drive_id)
