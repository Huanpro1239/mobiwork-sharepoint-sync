from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_storage import ImageSharePointClient


class ImageSharePointClientTests(unittest.TestCase):
    def make_client(self):
        client = object.__new__(ImageSharePointClient)
        client._put_content = Mock(return_value={"id": "image-1", "size": 3})
        return client

    def test_upload_bytes_exposes_stable_public_binary_contract(self):
        client = self.make_client()

        result = client.upload_bytes(
            "drive-1",
            "Data anh/2026-08/NV01/KH01/photo.jpg",
            b"abc",
            "image/jpeg",
        )

        self.assertEqual(result["id"], "image-1")
        client._put_content.assert_called_once_with(
            "drive-1",
            "Data anh/2026-08/NV01/KH01",
            "photo.jpg",
            b"abc",
            "image/jpeg",
        )

    def test_upload_bytes_rejects_invalid_path_and_empty_content(self):
        client = self.make_client()

        with self.assertRaises(ValueError):
            client.upload_bytes("drive-1", "photo.jpg", b"abc", "image/jpeg")
        with self.assertRaises(ValueError):
            client.upload_bytes(
                "drive-1",
                "Data anh/photo.jpg",
                b"",
                "image/jpeg",
            )


if __name__ == "__main__":
    unittest.main()
