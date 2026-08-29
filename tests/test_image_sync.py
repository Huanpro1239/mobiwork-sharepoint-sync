from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_sync import (
    ImageSyncConfig,
    _download_image,
    _host_allowed,
    _remote_image_path,
    _resolve_start_date,
    retained_months,
    run_image_sync,
)
from mobiwork import ReportConfig


class FakeSource:
    def __init__(self, records, session=None):
        self.records = records
        self.calls = []
        self._session = session

    @property
    def session(self):
        return self._session

    def fetch_report_range(self, cfg, from_date, to_date):
        self.calls.append((cfg.key, from_date, to_date))
        return list(self.records)


class FakeStorage:
    def __init__(self, state=None, children=None):
        self.state = state
        self.children = children or []
        self.deleted = []
        self.uploaded_json = []
        self.uploaded_bytes = []
        self.items = {}

    def download_json(self, drive_id, remote_path):
        return self.state

    def list_folder_children(self, drive_id, remote_folder):
        return list(self.children)

    def delete_path(self, drive_id, remote_path):
        self.deleted.append(remote_path)
        return True

    def upload_json(self, drive_id, remote_path, payload):
        self.uploaded_json.append((remote_path, payload))
        return {"id": "state"}

    def get_item_by_path(self, drive_id, remote_path):
        return self.items.get(remote_path)

    def upload_bytes(self, drive_id, remote_path, content, content_type="application/octet-stream"):
        self.uploaded_bytes.append((remote_path, content, content_type))
        return {"id": "image", "size": len(content)}


class FakeResponse(requests.Response):
    def __init__(self, url, content, content_type="image/jpeg", content_length=None):
        super().__init__()
        self.status_code = 200
        self.url = url
        self._payload = content
        self.headers["Content-Type"] = content_type
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def iter_content(self, chunk_size=1, decode_unicode=False):
        for index in range(0, len(self._payload), chunk_size):
            yield self._payload[index : index + chunk_size]


class FakeDownloadSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class ImageSyncTests(unittest.TestCase):
    def setUp(self):
        self.report = ReportConfig(
            key="visit",
            enabled=True,
            name="BaoCaoViengTham",
            folder="01_BaoCaoViengTham",
            url="https://example.invalid/visit",
        )

    def test_retains_current_and_previous_calendar_month(self):
        self.assertEqual(retained_months(date(2026, 8, 28)), {"2026-07", "2026-08"})
        self.assertEqual(retained_months(date(2026, 9, 1)), {"2026-08", "2026-09"})

    def test_first_run_backfills_from_start_of_previous_month(self):
        self.assertEqual(_resolve_start_date(date(2026, 8, 28), None), date(2026, 7, 1))

    def test_existing_state_uses_completed_cursor_with_one_day_overlap(self):
        self.assertEqual(
            _resolve_start_date(
                date(2026, 8, 28),
                {
                    "last_completed_sync_date": "2026-08-27",
                    "last_successful_sync_date": "2026-08-25",
                },
            ),
            date(2026, 8, 26),
        )

    def test_force_from_date_is_clamped_to_retention_window(self):
        self.assertEqual(
            _resolve_start_date(date(2026, 8, 28), None, date(2026, 6, 1)),
            date(2026, 7, 1),
        )

    def test_dry_run_counts_hinh_anh_links_without_downloading(self):
        source = FakeSource(
            [
                {
                    "ngay": "28/08/2026",
                    "hinh_anh": "https://dmsimages.mobiwork.vn/a.jpg; https://dmsimages.mobiwork.vn/b.png",
                    "ten_nhan_vien": "Nguyen Van A",
                    "ma_kh": "KH001",
                    "stt_hinh": 1,
                },
                {
                    "ngay": "30/06/2026",
                    "hinh_anh": "https://dmsimages.mobiwork.vn/old.jpg",
                    "ten_nhan_vien": "Nguyen Van B",
                    "ma_kh": "KH002",
                    "stt_hinh": 1,
                },
            ]
        )

        result = run_image_sync(
            reports=[self.report],
            source=source,
            storage=None,
            drive_id=None,
            dry_run=True,
            today=date(2026, 8, 28),
            cfg=ImageSyncConfig(),
        )

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["records_scanned"], 2)
        self.assertEqual(result["from_date"], "2026-07-01")
        self.assertEqual(source.calls[0][1:], (date(2026, 7, 1), date(2026, 8, 28)))
        self.assertIn("duration_seconds", result)

    def test_month_rollover_deletes_only_expired_month_folder(self):
        source = FakeSource([])
        storage = FakeStorage(
            state={"last_completed_sync_date": "2026-08-31"},
            children=[
                {"name": "2026-07", "folder": {}, "id": "jul"},
                {"name": "2026-08", "folder": {}, "id": "aug"},
                {"name": "2026-09", "folder": {}, "id": "sep"},
                {"name": "Manual", "folder": {}, "id": "manual"},
                {"name": "_state.json", "file": {}, "id": "state"},
            ],
        )

        result = run_image_sync(
            reports=[self.report],
            source=source,
            storage=storage,
            drive_id="drive",
            dry_run=False,
            today=date(2026, 9, 1),
            cfg=ImageSyncConfig(),
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["deleted_month_folders"], ["2026-07"])
        self.assertEqual(storage.deleted, ["Data anh/2026-07"])
        self.assertEqual(storage.uploaded_json[0][0], "Data anh/_state.json")
        state_payload = storage.uploaded_json[0][1]
        self.assertEqual(state_payload["schema_version"], 3)
        self.assertEqual(state_payload["last_completed_sync_date"], "2026-09-01")

    def test_remote_path_is_month_employee_customer_and_sanitized(self):
        folder, remote_path = _remote_image_path(
            ImageSyncConfig(),
            {"ten_nhan_vien": "NV / A", "ma_kh": "KH:01", "stt_hinh": 2},
            "https://dmsimages.mobiwork.vn/photo.jpg?token=abc",
            date(2026, 8, 28),
            1,
            ".jpg",
        )

        self.assertEqual(folder, "Data anh/2026-08/NV _ A/KH_01")
        self.assertTrue(remote_path.startswith(folder + "/KH_01_20260828_2_"))
        self.assertTrue(remote_path.endswith(".jpg"))

    def test_only_allow_listed_image_hosts(self):
        allowed = ("dmsimages.mobiwork.vn",)
        self.assertTrue(_host_allowed("dmsimages.mobiwork.vn", allowed))
        self.assertTrue(_host_allowed("cdn.dmsimages.mobiwork.vn", allowed))
        self.assertFalse(_host_allowed("example.com", allowed))

    def test_download_rejects_unapproved_host_before_network_call(self):
        source = FakeSource([], session=FakeDownloadSession(None))
        with self.assertRaisesRegex(ValueError, "allow-listed"):
            _download_image(source, "https://example.com/a.jpg", ImageSyncConfig())
        self.assertEqual(source.session.calls, [])

    def test_download_enforces_max_image_size(self):
        response = FakeResponse(
            "https://dmsimages.mobiwork.vn/a.jpg",
            b"\xff\xd8\xff" + b"x" * 32,
            content_length=35,
        )
        session = FakeDownloadSession(response)
        source = FakeSource([], session=session)
        cfg = ImageSyncConfig(max_image_bytes=10)

        with self.assertRaisesRegex(ValueError, "size limit"):
            _download_image(source, response.url, cfg)

    def test_successful_download_uses_streaming_and_detects_image_type(self):
        payload = b"\xff\xd8\xff" + b"jpeg-data"
        response = FakeResponse("https://dmsimages.mobiwork.vn/viewimage", payload)
        session = FakeDownloadSession(response)
        source = FakeSource([], session=session)

        content, content_type, extension = _download_image(
            source,
            response.url,
            ImageSyncConfig(),
        )

        self.assertEqual(content, payload)
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(extension, ".jpg")
        self.assertTrue(session.calls[0][1]["stream"])


if __name__ == "__main__":
    unittest.main()
