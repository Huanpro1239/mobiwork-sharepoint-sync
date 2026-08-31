from __future__ import annotations

import hashlib
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_sync import ImageSyncConfig
from image_sync_reliable import run_image_sync_reliable
from mobiwork import ReportConfig


class FakeSource:
    def __init__(self, records):
        self.records = list(records)
        self.calls = []
        self.session = None

    def fetch_report_range(self, cfg, from_date, to_date):
        self.calls.append((cfg.key, from_date, to_date))
        return list(self.records)


class FolderStorage:
    def __init__(self, state=None, folders=None):
        self.state = state
        self.folders = folders or {}
        self.list_calls = []
        self.exact_calls = []
        self.uploaded_json = []
        self.uploaded_bytes = []
        self.deleted = []

    def download_json(self, drive_id, remote_path):
        return self.state

    def list_folder_children(self, drive_id, remote_folder):
        self.list_calls.append(remote_folder)
        return list(self.folders.get(remote_folder, []))

    def delete_path(self, drive_id, remote_path):
        self.deleted.append(remote_path)
        return True

    def upload_json(self, drive_id, remote_path, payload):
        self.uploaded_json.append((remote_path, payload))
        self.state = payload
        return {"id": "state"}

    def get_item_by_path(self, drive_id, remote_path):
        self.exact_calls.append(remote_path)
        return None

    def upload_bytes(self, drive_id, remote_path, content, content_type="application/octet-stream"):
        self.uploaded_bytes.append((remote_path, content, content_type))
        folder, _, name = remote_path.rpartition("/")
        self.folders.setdefault(folder, []).append(
            {"name": name, "file": {}, "size": len(content), "id": name}
        )
        return {"id": name, "size": len(content)}


class ReliableImageSyncTests(unittest.TestCase):
    def setUp(self):
        self.report = ReportConfig(
            key="visit",
            enabled=True,
            name="BaoCaoViengTham",
            folder="01_BaoCaoViengTham",
            url="https://example.invalid/visit",
        )

    @staticmethod
    def record(url, day="2026-08-20", sequence=1):
        return {
            "_sync_date": day,
            "ngay": day,
            "hinh_anh": url,
            "ten_nhan_vien": "NV A",
            "ma_kh": "KH001",
            "stt_hinh": sequence,
        }

    def test_existing_digest_skips_even_when_sequence_and_extension_changed(self):
        url = "https://dmsimages.mobiwork.vn/photo/view?id=123"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        folder = "Data anh/2026-08/NV A/KH001"
        storage = FolderStorage(
            folders={
                folder: [
                    {
                        "name": f"KH001_20260820_999_{digest}.png",
                        "file": {},
                        "size": 1234,
                    }
                ]
            }
        )

        with patch("image_sync_reliable._download_image") as download:
            result = run_image_sync_reliable(
                reports=[self.report],
                source=FakeSource([self.record(url, sequence=1)]),
                storage=storage,
                drive_id="drive",
                dry_run=False,
                today=date(2026, 8, 31),
                cfg=ImageSyncConfig(),
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["skipped_existing_count"], 1)
        self.assertEqual(result["uploaded_count"], 0)
        self.assertEqual(result["pending_remaining"], 0)
        self.assertEqual(result["completeness_pct"], 100.0)
        download.assert_not_called()
        self.assertEqual(storage.list_calls.count(folder), 1)
        self.assertEqual(storage.exact_calls, [])

    def test_batch_limit_preserves_retry_cursor_and_reports_warming_up(self):
        urls = [
            "https://dmsimages.mobiwork.vn/a.jpg",
            "https://dmsimages.mobiwork.vn/b.jpg",
        ]
        storage = FolderStorage(
            state={
                "last_completed_sync_date": "2026-08-19",
                "last_successful_sync_date": "2026-08-19",
            }
        )
        source = FakeSource(
            [self.record(urls[0], "2026-08-20", 1), self.record(urls[1], "2026-08-21", 2)]
        )

        with patch.dict(os.environ, {"IMAGE_SYNC_MAX_UPLOADS_PER_RUN": "1"}, clear=False), patch(
            "image_sync_reliable._download_image",
            return_value=(b"\xff\xd8\xffimage", "image/jpeg", ".jpg"),
        ):
            result = run_image_sync_reliable(
                reports=[self.report],
                source=source,
                storage=storage,
                drive_id="drive",
                dry_run=False,
                today=date(2026, 8, 31),
                cfg=ImageSyncConfig(),
            )

        self.assertEqual(result["status"], "warming_up")
        self.assertEqual(result["uploaded_count"], 1)
        self.assertEqual(result["deferred_count"], 1)
        self.assertEqual(result["pending_remaining"], 1)
        self.assertEqual(result["retry_from_date"], "2026-08-21")
        state = storage.uploaded_json[-1][1]
        self.assertEqual(state["schema_version"], 4)
        self.assertEqual(state["last_completed_sync_date"], "2026-08-19")
        self.assertEqual(state["retry_from_date"], "2026-08-21")

    def test_next_run_reuses_uploaded_digest_and_finishes_without_duplicate_upload(self):
        urls = [
            "https://dmsimages.mobiwork.vn/a.jpg",
            "https://dmsimages.mobiwork.vn/b.jpg",
        ]
        records = [self.record(urls[0], "2026-08-20", 1), self.record(urls[1], "2026-08-21", 2)]
        storage = FolderStorage(
            state={
                "last_completed_sync_date": "2026-08-19",
                "last_successful_sync_date": "2026-08-19",
            }
        )

        download_result = (b"\xff\xd8\xffimage", "image/jpeg", ".jpg")
        with patch.dict(os.environ, {"IMAGE_SYNC_MAX_UPLOADS_PER_RUN": "1"}, clear=False), patch(
            "image_sync_reliable._download_image", return_value=download_result
        ):
            first = run_image_sync_reliable(
                [self.report], FakeSource(records), storage, "drive", False, date(2026, 8, 31), ImageSyncConfig()
            )
        self.assertEqual(first["status"], "warming_up")
        first_uploads = len(storage.uploaded_bytes)

        with patch.dict(os.environ, {"IMAGE_SYNC_MAX_UPLOADS_PER_RUN": "1"}, clear=False), patch(
            "image_sync_reliable._download_image", return_value=download_result
        ):
            second = run_image_sync_reliable(
                [self.report], FakeSource(records), storage, "drive", False, date(2026, 8, 31), ImageSyncConfig()
            )

        self.assertEqual(second["status"], "success")
        self.assertEqual(second["pending_remaining"], 0)
        self.assertEqual(second["completeness_pct"], 100.0)
        self.assertEqual(len(storage.uploaded_bytes) - first_uploads, 1)
        self.assertEqual(storage.state["last_completed_sync_date"], "2026-08-31")

    def test_duplicate_rows_for_same_folder_and_url_are_one_target(self):
        url = "https://dmsimages.mobiwork.vn/same.jpg"
        source = FakeSource([self.record(url, sequence=1), self.record(url, sequence=99)])
        storage = FolderStorage()

        with patch(
            "image_sync_reliable._download_image",
            return_value=(b"\xff\xd8\xffimage", "image/jpeg", ".jpg"),
        ):
            result = run_image_sync_reliable(
                [self.report], source, storage, "drive", False, date(2026, 8, 31), ImageSyncConfig()
            )

        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["unique_target_count"], 1)
        self.assertEqual(result["duplicate_candidate_count"], 1)
        self.assertEqual(result["uploaded_count"], 1)
        self.assertEqual(result["status"], "success")


if __name__ == "__main__":
    unittest.main()
