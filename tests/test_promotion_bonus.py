from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from promotion_bonus import (
    PromotionBonusConfig,
    build_frames,
    fetch_programs,
    fetch_snapshot,
    load_config,
    write_workbook,
)


class FakeMobiWork:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get_json(self, url, params=None, *, operation_key="api", request_number=1):
        self.calls.append(
            {
                "url": url,
                "params": dict(params or {}),
                "operation_key": operation_key,
                "request_number": request_number,
            }
        )
        if not self.payloads:
            raise AssertionError("Unexpected API request")
        return self.payloads.pop(0)


def config() -> PromotionBonusConfig:
    return PromotionBonusConfig(
        enabled=True,
        name="BaoCaoTraThuong",
        folder="06_BaoCaoTraThuong",
        filename="BaoCaoTraThuong_Current.xlsx",
        catalog_url="https://example.test/PromotionBonus",
        report_url="https://example.test/PromotionBonusReport",
        catalog_page_size=2,
    )


class PromotionBonusTests(unittest.TestCase):
    def test_repository_config_matches_openapi_contract(self):
        cfg = load_config()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.folder, "06_BaoCaoTraThuong")
        self.assertEqual(cfg.filename, "BaoCaoTraThuong_Current.xlsx")
        self.assertTrue(cfg.catalog_url.endswith("/OpenAPI/V1/PromotionBonus"))
        self.assertTrue(cfg.report_url.endswith("/OpenAPI/V1/PromotionBonusReport"))
        self.assertLessEqual(cfg.catalog_page_size, 200)

    def test_catalog_paginates_to_api_total(self):
        client = FakeMobiWork(
            [
                {
                    "status": True,
                    "total": 3,
                    "data": [
                        {"_id": "p1", "name": "CT 1"},
                        {"_id": "p2", "name": "CT 2"},
                    ],
                },
                {
                    "status": True,
                    "total": 3,
                    "data": [{"_id": "p3", "name": "CT 3"}],
                },
            ]
        )

        programs = fetch_programs(client, config())

        self.assertEqual([item["_id"] for item in programs], ["p1", "p2", "p3"])
        self.assertEqual([call["params"]["page_number"] for call in client.calls], [1, 2])
        self.assertTrue(all(call["params"]["page_size"] == 2 for call in client.calls))

    def test_report_uses_one_id_per_request_and_preserves_program_provenance(self):
        programs = [
            {"_id": "p1", "name": "CT 1"},
            {"_id": "p2", "name": "CT 2"},
        ]
        client = FakeMobiWork(
            [
                {
                    "status": True,
                    "total": 1,
                    "data": [{"customer": "A", "amount": 100}],
                    "arrChiTieu": [{"metric": "SL"}],
                    "arrTraThuong": [{"level": 1, "bonus": 10}],
                },
                {
                    "status": True,
                    "total": 1,
                    "data": [{"customer": "B", "amount": 200}],
                    "arrChiTieu": [{"metric": "DT"}],
                    "arrTraThuong": [{"level": 2, "bonus": 20}],
                },
            ]
        )

        snapshot = fetch_snapshot(client, config(), programs)

        self.assertEqual(
            [call["params"]["id_ct"] for call in client.calls],
            ["p1", "p2"],
        )
        self.assertEqual(
            [row["promotion_program_id"] for row in snapshot["data"]],
            ["p1", "p2"],
        )
        self.assertEqual(snapshot["targets"][0]["promotion_program_name"], "CT 1")
        self.assertEqual(snapshot["rewards"][1]["promotion_program_name"], "CT 2")

    def test_report_total_mismatch_is_rejected(self):
        client = FakeMobiWork(
            [
                {
                    "status": True,
                    "total": 2,
                    "data": [{"customer": "A"}],
                    "arrChiTieu": [],
                    "arrTraThuong": [],
                }
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "API total=2, fetched=1"):
            fetch_snapshot(client, config(), [{"_id": "p1", "name": "CT 1"}])

    def test_workbook_contains_four_snapshot_sheets(self):
        snapshot = {
            "programs": [{"_id": "p1", "name": "CT 1", "ptype": {"label": "Mua bán"}}],
            "data": [
                {
                    "promotion_program_id": "p1",
                    "promotion_program_name": "CT 1",
                    "customer": "A",
                    "nested": [{"x": 1}],
                }
            ],
            "targets": [],
            "rewards": [{"promotion_program_id": "p1", "bonus": 10}],
        }
        frames = build_frames(snapshot)
        self.assertEqual(
            set(frames),
            {"ChuongTrinh", "Data", "ChiTieu", "TraThuong"},
        )
        self.assertEqual(frames["ChuongTrinh"].iloc[0]["ptype_label"], "Mua bán")

        with tempfile.TemporaryDirectory() as tmp:
            path = write_workbook(
                frames,
                "BaoCaoTraThuong_Current.xlsx",
                Path(tmp),
            )
            sheets = pd.ExcelFile(path, engine="openpyxl").sheet_names
            self.assertEqual(
                sheets,
                ["ChuongTrinh", "Data", "ChiTieu", "TraThuong"],
            )


if __name__ == "__main__":
    unittest.main()
