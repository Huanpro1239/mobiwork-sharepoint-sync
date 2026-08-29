from __future__ import annotations

import sys
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mobiwork import ReportConfig
from sharepoint_image_source import SharePointMonthlyImageSource


class FakeMobiWork:
    def __init__(self):
        self.session = object()


class FakeSharePoint:
    def __init__(self, files, children=None):
        self.files = files
        self.children = children or {}

    def get_item_by_path(self, drive_id, remote_path):
        if remote_path in self.files:
            return {"name": remote_path.rsplit("/", 1)[-1], "file": {}, "size": len(self.files[remote_path])}
        return None

    def list_folder_children(self, drive_id, remote_folder):
        return list(self.children.get(remote_folder, []))

    def download_file_bytes(self, drive_id, remote_path):
        return self.files.get(remote_path)


def workbook_bytes(rows):
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name="Data", index=False)
    return buffer.getvalue()


class SharePointMonthlyImageSourceTests(unittest.TestCase):
    def setUp(self):
        self.report = ReportConfig(
            key="visit",
            enabled=True,
            name="BaoCaoViengTham",
            folder="01_BaoCaoViengTham",
            url="https://example.invalid/visit",
        )

    def test_reads_canonical_monthly_master(self):
        path = "01_BaoCaoViengTham/2026/08/BaoCaoViengTham_2026-08.xlsx"
        sharepoint = FakeSharePoint(
            {
                path: workbook_bytes(
                    [
                        {
                            "_sync_date": "2026-08-29",
                            "ngay": "2026-08-28T17:00:00.000Z",
                            "ma_kh": "KH001",
                            "hinh_anh": "['http://dmsimages.mobiwork.vn/a.jpg']",
                        }
                    ]
                )
            }
        )
        source = SharePointMonthlyImageSource(FakeMobiWork(), sharepoint, "drive")

        rows = source.fetch_report_range(
            self.report,
            date(2026, 8, 28),
            date(2026, 8, 29),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ma_kh"], "KH001")
        self.assertEqual(source.source_files, [path])
        self.assertIs(source.session, source.mobiwork.session)

    def test_falls_back_to_legacy_history_workbook(self):
        folder = "01_BaoCaoViengTham/2026/07"
        history_name = "BaoCaoViengTham_History_2026-07-01_to_2026-07-31.xlsx"
        history_path = f"{folder}/{history_name}"
        sharepoint = FakeSharePoint(
            {
                history_path: workbook_bytes(
                    [{"ngay": "2026-07-20", "ma_kh": "KH002", "hinh_anh": ""}]
                )
            },
            children={
                folder: [
                    {"name": history_name, "file": {}, "id": "history"},
                    {"name": "notes.txt", "file": {}, "id": "notes"},
                ]
            },
        )
        source = SharePointMonthlyImageSource(FakeMobiWork(), sharepoint, "drive")

        rows = source.fetch_report_range(
            self.report,
            date(2026, 7, 1),
            date(2026, 7, 31),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ma_kh"], "KH002")
        self.assertEqual(source.source_files, [history_path])

    def test_raises_when_month_source_is_missing(self):
        source = SharePointMonthlyImageSource(FakeMobiWork(), FakeSharePoint({}), "drive")

        with self.assertRaises(FileNotFoundError):
            source.fetch_report_range(
                self.report,
                date(2026, 8, 1),
                date(2026, 8, 29),
            )


if __name__ == "__main__":
    unittest.main()
