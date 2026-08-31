"""One-time bootstrap of private AI assets from the legacy DMS folder to SharePoint."""
from __future__ import annotations

import argparse
import mimetypes
import os
from pathlib import Path

from image_storage import ImageSharePointClient


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _resolve_drive(client: ImageSharePointClient) -> str:
    configured = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
    return configured or client.get_drive_id(client.get_site_id())


def _find_one(root: Path, patterns: tuple[str, ...]) -> Path:
    for pattern in patterns:
        matches = sorted(path for path in root.glob(pattern) if path.is_file())
        if matches:
            return matches[0]
    raise FileNotFoundError(f"Không tìm thấy asset theo mẫu {patterns} trong {root}")


def _reference_dir(root: Path) -> Path:
    candidates = [root / "Nguoi cham", root / "reference", root / "references"]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("Không tìm thấy thư mục ảnh mẫu Nguoi cham/reference")


def _collect(source: Path) -> list[tuple[Path, str]]:
    reference = _reference_dir(source)
    weight = _find_one(source, ("weights/yolov8s-world.pt", "**/yolov8s-world.pt"))
    override = _find_one(source, ("reference_overrides.csv", "**/reference_overrides.csv"))
    template = _find_one(
        source,
        (
            "*Check_Bang_cham_cong_va_thuong_KPI*.xlsx",
            "**/*Check_Bang_cham_cong_va_thuong_KPI*.xlsx",
            "**/KPI_template.xlsx",
        ),
    )
    assets: list[tuple[Path, str]] = [
        (weight, "weights/yolov8s-world.pt"),
        (override, "reference_overrides.csv"),
        (template, "template/KPI_template.xlsx"),
    ]
    for path in sorted(reference.rglob("*")):
        if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES:
            assets.append((path, f"reference/{path.relative_to(reference).as_posix()}"))
    if len(assets) <= 3:
        raise RuntimeError(f"Thư mục reference không có ảnh hợp lệ: {reference}")
    return assets


def _content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload private V2.3/V2.4 assets to SharePoint")
    parser.add_argument("--source", required=True, help="Legacy DMS project directory")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        raise SystemExit(f"Source directory not found: {source}")
    remote_root = os.environ.get("AI_SHAREPOINT_ASSET_ROOT", "Model Assets").strip().strip("/")
    assets = _collect(source)
    print(f"assets={len(assets)} remote_root={remote_root}")
    if args.dry_run:
        for local, remote in assets:
            print(f"DRY-RUN {local} -> {remote_root}/{remote}")
        return 0

    client = ImageSharePointClient.from_env()
    drive_id = _resolve_drive(client)
    for index, (local, relative) in enumerate(assets, start=1):
        remote = f"{remote_root}/{relative}"
        client.upload_bytes(drive_id, remote, local.read_bytes(), _content_type(local))
        print(f"[{index}/{len(assets)}] uploaded {remote}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
