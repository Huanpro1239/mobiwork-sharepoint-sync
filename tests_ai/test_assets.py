from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scoring.assets import SharePointAssetManager


class FakeAssetClient:
    def __init__(self):
        self.files = {
            "Model Assets/reference/Dat/Bien hieu/new.jpg": b"new-image",
            "Model Assets/reference_overrides.csv": b"relative_path,action\n",
            "Model Assets/weights/yolov8s-world.pt": b"weights",
            "Model Assets/template/KPI_template.xlsx": b"template",
        }

    def get_item_by_path(self, _drive_id: str, path: str):
        if path in {"Model Assets/reference", "Model Assets/reference/Dat", "Model Assets/reference/Dat/Bien hieu"}:
            return {"id": path, "folder": {}}
        if path in self.files:
            return {"id": path, "size": len(self.files[path]), "eTag": f"etag-{path}"}
        return None

    def list_folder_children(self, _drive_id: str, path: str):
        if path == "Model Assets/reference":
            return [{"name": "Dat", "folder": {}}]
        if path == "Model Assets/reference/Dat":
            return [{"name": "Bien hieu", "folder": {}}]
        if path == "Model Assets/reference/Dat/Bien hieu":
            return [
                {
                    "name": "new.jpg",
                    "size": len(self.files["Model Assets/reference/Dat/Bien hieu/new.jpg"]),
                    "eTag": "etag-new",
                }
            ]
        return []

    def download_file_bytes(self, _drive_id: str, path: str):
        return self.files.get(path)


class AssetMirrorTests(unittest.TestCase):
    def test_sync_downloads_current_assets_and_removes_stale_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset_root = root / "ai"
            reference = asset_root / "reference"
            weights = asset_root / "weights"
            template = asset_root / "template" / "KPI_template.xlsx"
            stale = reference / "Dat" / "Bien hieu" / "old.jpg"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"old")
            manifest = asset_root / ".asset_manifest.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                '{"Model Assets/reference/Dat/Bien hieu/old.jpg":{"etag":"old","size":3}}',
                encoding="utf-8",
            )

            with (
                patch("scoring.assets.AI_ASSET_ROOT", asset_root),
                patch("scoring.assets.REFERENCE_DIR", reference),
                patch("scoring.assets.WEIGHTS_DIR", weights),
                patch("scoring.assets.TEMPLATE_EXCEL", template),
                patch("scoring.assets.ensure_runtime_dirs"),
            ):
                manager = SharePointAssetManager(FakeAssetClient(), "drive")
                manager.manifest_path = manifest
                result = manager.sync_required_assets()

            self.assertEqual(result.removed, 1)
            self.assertFalse(stale.exists())
            self.assertEqual(
                (reference / "Dat" / "Bien hieu" / "new.jpg").read_bytes(),
                b"new-image",
            )
            self.assertEqual((reference / "reference_overrides.csv").read_bytes(), b"relative_path,action\n")
            self.assertEqual((weights / "yolov8s-world.pt").read_bytes(), b"weights")
            self.assertEqual(template.read_bytes(), b"template")


if __name__ == "__main__":
    unittest.main()
