from __future__ import annotations

import unittest
from types import SimpleNamespace

import pandas as pd

from sharepoint_kpi_source import SharePointMonthlyKPISource


class SharePointKPISourceDateTests(unittest.TestCase):
    def _source(self):
        reports = [
            SimpleNamespace(key="visit", name="Visit", folder="visit"),
            SimpleNamespace(key="order", name="Order", folder="order"),
        ]
        return SharePointMonthlyKPISource(object(), "drive", reports)

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


if __name__ == "__main__":
    unittest.main()
