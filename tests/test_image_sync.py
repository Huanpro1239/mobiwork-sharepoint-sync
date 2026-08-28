from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_sync import (
    ImageSyncConfig,
    _remote_image_path,
    _resolve_start_date,
    retained_months,
    run_image_sync,
)
from mobiwork import ReportConfig


class FakeMobiWork:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def fetch_report_range(self, cfg, from_date, to_date):
        self.calls.append((cfg.key, from_date, to_date))
        return list(self.records)


class FakeSharePoint:
    def __init__(self, state=None, children=None):
        self.state = state
        self.children = children or []
        self.deleted = []
        self.uploaded_json = []

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
        self.assertEqual(
            retained_months(date(2026, 8, 28)),
            {"2026-07", "2026-08"},
        )
        self.assertEqual(
            retained_months(date(2026, 9, 1)),
            {"2026-08", "2026-09"},
        )

    def test_first_run_backfills_from_start_of_previous_month(self):
        self.assertEqual(
            _resolve_start_date(date(2026, 8, 28), None),
            date(2026, 7, 1),
        )

    def test_existing_state_uses_one_day_overlap(self):
        self.assertEqual(
            _resolve_start_date(
                date(2026, 8, 28),
                {"last_successful_sync_date": "2026-08-27"},
            ),
            date(2026, 8, 26),
        )

    def test_dry_run_counts_hinh_anh_links_without_downloading(self):
        mobiwork = FakeMobiWork(
            [
                {
                    "ngay": "28/08/2026",
                    "hinh_anh": (
                        "https://img.example/a.jpg; https://img.example/b.png"
                    ),
                    "ten_nhan_vien": "Nguyen Van A",
                    "ma_kh": "KH001",
                    "stt_hinh": 1,
                },
                {
                    "ngay": "30/06/2026",
                    "hinh_anh": "https://img.example/old.jpg",
                    "ten_nhan_vien": "Nguyen Van B",
                    "ma_kh": "KH002",
                    "stt_hinh": 1,
                },
            ]
        )

        result = run_image_sync(
            reports=[self.report],
            mobiwork=mobiwork,
            sharepoint=None,
            drive_id=None,
            dry_run=True,
            today=date(2026, 8, 28),
            cfg=ImageSyncConfig(),
        )

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["records_scanned"], 2)
        self.assertEqual(result["from_date"], "2026-07-01")
        self.assertEqual(
            mobiwork.calls[0][1:],
            (date(2026, 7, 1), date(2026, 8, 28)),
        )

    def test_month_rollover_deletes_only_expired_month_folder(self):
        mobiwork = FakeMobiWork([])
        sharepoint = FakeSharePoint(
            state={"last_successful_sync_date": "2026-08-31"},
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
            mobiwork=mobiwork,
            sharepoint=sharepoint,
            drive_id="drive",
            dry_run=False,
            today=date(2026, 9, 1),
            cfg=ImageSyncConfig(),
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["deleted_month_folders"], ["2026-07"])
        self.assertEqual(sharepoint.deleted, ["Data anh/2026-07"])
        self.assertEqual(sharepoint.uploaded_json[0][0], "Data anh/_state.json")

    def test_remote_path_is_month_employee_customer_and_sanitized(self):
        folder, remote_path = _remote_image_path(
            ImageSyncConfig(),
            {
                "ten_nhan_vien": "NV / A",
                "ma_kh": "KH:01",
                "stt_hinh": 2,
            },
            "https://img.example/photo.jpg?token=abc",
            date(2026, 8, 28),
            1,
            ".jpg",
        )

        self.assertEqual(folder, "Data anh/2026-08/NV _ A/KH_01")
        self.assertTrue(remote_path.startswith(folder + "/KH_01_20260828_2_"))
        self.assertTrue(remote_path.endswith(".jpg"))


if __name__ == "__main__":
    unittest.main()
