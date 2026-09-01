from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace

import pandas as pd

from image_sync import ImageSyncConfig
from sharepoint_kpi_source import SharePointMonthlyKPISource


class FakeImageSharePoint:
    def __init__(self, folders=None, exact=None):
        self.folders = folders or {}
        self.exact = exact or {}

    def get_item_by_path(self, _drive_id, path):
        return self.exact.get(path)

    def list_folder_children(self, _drive_id, folder):
        return list(self.folders.get(folder, []))


class SharePointKPISourceDateTests(unittest.TestCase):
    def _source(self, sharepoint=None):
        reports = [
            SimpleNamespace(key="visit", name="Visit", folder="visit"),
            SimpleNamespace(key="order", name="Order", folder="order"),
        ]
        return SharePointMonthlyKPISource(sharepoint or object(), "drive", reports)

    def test_recent_images_use_sync_date_not_utc_rollover(self):
        visits = pd.DataFrame(
            [
                {
                    "_sync_date": "2026-08-31",
                    "ngay": "2026-08-31T17:00:00.000Z",
                    "hinh_anh": "https://dmsimages.mobiwork.vn/viewimage?url=Files/a.jpg",
                },
                {
                    "_sync_date": "2026-06-30",
                    "ngay": "2026-06-30T17:00:00.000Z",
                    "hinh_anh": "https://dmsimages.mobiwork.vn/viewimage?url=Files/old.jpg",
                },
            ]
        )
        rows = self._source().recent_image_rows(
            visits,
            pd.Timestamp("2026-08-30", tz="Asia/Ho_Chi_Minh"),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["_sync_date"], "2026-08-31")

    def test_duplicate_url_inside_one_visit_is_emitted_once(self):
        url = "https://dmsimages.mobiwork.vn/viewimage?url=Files/a.jpg"
        visits = pd.DataFrame(
            [
                {
                    "_sync_date": "2026-08-01",
                    "ngay": "2026-08-01T17:00:00.000Z",
                    "hinh_anh": f"['{url}', '{url}']",
                }
            ]
        )
        rows = self._source().recent_image_rows(visits, pd.Timestamp("2026-08-15"))
        self.assertEqual([row["hinh_anh"] for row in rows], [url])

    def test_resolve_image_path_uses_url_digest_when_sequence_or_extension_changes(self):
        url = "https://dmsimages.mobiwork.vn/photos/example?id=123"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        folder = "Data anh/2026-08/Nguyen Van A/KH001"
        stored_name = f"KH001_20260829_1_{digest}.png"
        sharepoint = FakeImageSharePoint(
            folders={
                folder: [
                    {"name": stored_name, "file": {}, "size": 1234},
                ]
            }
        )
        source = self._source(sharepoint)
        row = {
            "_sync_date": "2026-08-29",
            "ngay": "2026-08-29",
            "ten_nhan_vien": "Nguyen Van A",
            "ma_kh": "KH001",
            "stt_hinh": "999",
            "hinh_anh": url,
            "_image_index": 7,
        }

        resolved = source.resolve_image_path(row, ImageSyncConfig())

        self.assertEqual(resolved, f"{folder}/{stored_name}")

    def test_resolve_image_path_does_not_cross_business_date(self):
        url = "https://dmsimages.mobiwork.vn/photos/example?id=456"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        folder = "Data anh/2026-08/Nguyen Van A/KH001"
        sharepoint = FakeImageSharePoint(
            folders={
                folder: [
                    {
                        "name": f"KH001_20260828_1_{digest}.jpg",
                        "file": {},
                        "size": 1234,
                    },
                ]
            }
        )
        source = self._source(sharepoint)
        row = {
            "_sync_date": "2026-08-29",
            "ngay": "2026-08-29",
            "ten_nhan_vien": "Nguyen Van A",
            "ma_kh": "KH001",
            "hinh_anh": url,
            "_image_index": 1,
        }

        with self.assertRaises(FileNotFoundError):
            source.resolve_image_path(row, ImageSyncConfig())


if __name__ == "__main__":
    unittest.main()
