from __future__ import annotations

import hashlib
import unittest

from image_sync import ImageSyncConfig
from scoring.cloud_image_path import _digest_fallback_path


class _FakeSource:
    def __init__(self, children):
        self.children = children
        self.requested_folders = []

    def _children(self, folder):
        self.requested_folders.append(folder)
        return list(self.children)


class CloudImagePathTests(unittest.TestCase):
    def test_fallback_ignores_sequence_and_extension_but_keeps_url_and_date(self):
        url = "https://dmsimages.mobiwork.vn/photos/example?id=123"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        source = _FakeSource(
            [
                {"name": f"KH001_20260829_1_{digest}.png", "size": 10},
                {"name": "KH001_20260829_2_aaaaaaaaaa.jpg", "size": 20},
            ]
        )
        row = {
            "_sync_date": "2026-08-29",
            "ten_nhan_vien": "Nguyen Van A",
            "ma_kh": "KH001",
            "stt_hinh": "999",
            "hinh_anh": url,
            "_image_index": 7,
        }

        resolved = _digest_fallback_path(source, row, ImageSyncConfig())

        self.assertEqual(
            resolved,
            f"Data anh/2026-08/Nguyen Van A/KH001/KH001_20260829_1_{digest}.png",
        )
        self.assertEqual(
            source.requested_folders,
            ["Data anh/2026-08/Nguyen Van A/KH001"],
        )

    def test_fallback_does_not_cross_business_date(self):
        url = "https://dmsimages.mobiwork.vn/photos/example?id=456"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        source = _FakeSource(
            [{"name": f"KH001_20260828_1_{digest}.jpg", "size": 10}]
        )
        row = {
            "_sync_date": "2026-08-29",
            "ten_nhan_vien": "Nguyen Van A",
            "ma_kh": "KH001",
            "hinh_anh": url,
            "_image_index": 1,
        }

        with self.assertRaises(FileNotFoundError):
            _digest_fallback_path(source, row, ImageSyncConfig())


if __name__ == "__main__":
    unittest.main()
