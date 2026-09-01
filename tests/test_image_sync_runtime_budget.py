from __future__ import annotations

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
        self.session = None

    def fetch_report_range(self, cfg, from_date, to_date):
        return list(self.records)


class FakeStorage:
    def __init__(self):
        self.state = {
            "last_completed_sync_date": "2026-08-19",
            "last_successful_sync_date": "2026-08-19",
        }
        self.folders = {}
        self.uploaded_bytes = []
        self.uploaded_json = []

    def download_json(self, drive_id, remote_path):
        return self.state

    def list_folder_children(self, drive_id, remote_folder):
        return list(self.folders.get(remote_folder, []))

    def get_item_by_path(self, drive_id, remote_path):
        return None

    def upload_bytes(self, drive_id, remote_path, content, content_type="application/octet-stream"):
        self.uploaded_bytes.append((remote_path, content, content_type))
        folder, _, name = remote_path.rpartition("/")
        self.folders.setdefault(folder, []).append(
            {"name": name, "file": {}, "size": len(content), "id": name}
        )
        return {"id": name, "size": len(content)}

    def upload_json(self, drive_id, remote_path, payload):
        self.uploaded_json.append((remote_path, payload))
        self.state = payload
        return {"id": "state"}

    def delete_path(self, drive_id, remote_path):
        return True


class ImageSyncRuntimeBudgetTests(unittest.TestCase):
    @staticmethod
    def record(url: str, day: str, sequence: int):
        return {
            "_sync_date": day,
            "ngay": day,
            "hinh_anh": url,
            "ten_nhan_vien": "NV A",
            "ma_kh": "KH001",
            "stt_hinh": sequence,
        }

    def test_runtime_budget_checkpoints_remaining_work_as_warming_up(self):
        report = ReportConfig(
            key="visit",
            enabled=True,
            name="BaoCaoViengTham",
            folder="01_BaoCaoViengTham",
            url="https://example.invalid/visit",
        )
        records = [
            self.record("https://dmsimages.mobiwork.vn/a.jpg", "2026-08-20", 1),
            self.record("https://dmsimages.mobiwork.vn/b.jpg", "2026-08-21", 2),
            self.record("https://dmsimages.mobiwork.vn/c.jpg", "2026-08-22", 3),
        ]
        storage = FakeStorage()

        with patch.dict(
            os.environ,
            {
                "IMAGE_SYNC_MAX_RUNTIME_SECONDS": "600",
                "IMAGE_SYNC_MAX_UPLOADS_PER_RUN": "10",
            },
            clear=False,
        ), patch(
            "image_sync_reliable.time.monotonic",
            side_effect=[0.0, 0.0, 601.0, 601.0, 601.0],
        ), patch(
            "image_sync_reliable._download_image",
            return_value=(b"\xff\xd8\xffimage", "image/jpeg", ".jpg"),
        ):
            result = run_image_sync_reliable(
                reports=[report],
                source=FakeSource(records),
                storage=storage,
                drive_id="drive",
                dry_run=False,
                today=date(2026, 8, 31),
                cfg=ImageSyncConfig(),
            )

        self.assertEqual(result["status"], "warming_up")
        self.assertEqual(result["uploaded_count"], 1)
        self.assertEqual(result["deferred_count"], 2)
        self.assertEqual(result["pending_remaining"], 2)
        self.assertEqual(result["retry_from_date"], "2026-08-21")
        self.assertTrue(result["runtime_budget_exhausted"])
        self.assertEqual(result["stop_reason"], "runtime_budget")
        self.assertEqual(result["runtime_limit_seconds"], 600)

        state = storage.uploaded_json[-1][1]
        self.assertEqual(state["last_completed_sync_date"], "2026-08-19")
        self.assertEqual(state["retry_from_date"], "2026-08-21")
        self.assertTrue(state["runtime_budget_exhausted"])
        self.assertEqual(state["stop_reason"], "runtime_budget")


if __name__ == "__main__":
    unittest.main()
