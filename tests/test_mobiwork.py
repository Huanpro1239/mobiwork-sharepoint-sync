import json
import unittest
from datetime import date

import requests

from src.mobiwork import (
    MobiWorkClient,
    ReportConfig,
    expand_records,
    get_by_path,
    validate_records,
)


class GetByPathTests(unittest.TestCase):
    def test_nested_path(self):
        payload = {"data": {"items": [{"id": 1}]}}
        self.assertEqual(get_by_path(payload, "data.items"), [{"id": 1}])

    def test_missing_path(self):
        self.assertIsNone(get_by_path({"data": {}}, "data.items"))

    def test_empty_path_returns_payload(self):
        payload = {"ok": True}
        self.assertIs(get_by_path(payload, None), payload)


class ExpandRecordsTests(unittest.TestCase):
    def test_visit_rows_inherit_employee_fields(self):
        records = [
            {
                "ma_nv": "NV01",
                "ten_nhan_vien": "Nguyen Van A",
                "thoi_gian_vt": [
                    {"ma_kh": "KH01", "checkin": "08:00"},
                    {"ma_kh": "KH02", "checkin": "09:00"},
                ],
            }
        ]

        result = expand_records(records, "thoi_gian_vt")

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["ma_nv"], "NV01")
        self.assertEqual(result[0]["ten_nhan_vien"], "Nguyen Van A")
        self.assertEqual(result[0]["ma_kh"], "KH01")
        self.assertEqual(result[1]["ma_kh"], "KH02")
        self.assertNotIn("thoi_gian_vt", result[0])

    def test_empty_nested_list_produces_no_visit_rows(self):
        records = [{"ma_nv": "NV01", "thoi_gian_vt": []}]
        self.assertEqual(expand_records(records, "thoi_gian_vt"), [])

    def test_non_object_child_is_rejected(self):
        with self.assertRaises(TypeError):
            expand_records([{"thoi_gian_vt": ["bad"]}], "thoi_gian_vt")


class ValidateRecordsTests(unittest.TestCase):
    def test_duplicate_primary_key_is_rejected(self):
        cfg = ReportConfig(
            key="bill",
            enabled=True,
            name="Bill",
            folder="Bill",
            primary_key=["ma_phieu"],
        )
        with self.assertRaisesRegex(ValueError, "duplicate primary key"):
            validate_records(
                [{"ma_phieu": "A"}, {"ma_phieu": "A"}],
                cfg,
            )

    def test_missing_required_field_is_rejected(self):
        cfg = ReportConfig(
            key="bill",
            enabled=True,
            name="Bill",
            folder="Bill",
            required_fields=["ma_phieu", "san_pham"],
        )
        with self.assertRaisesRegex(ValueError, "missing required fields"):
            validate_records([{"ma_phieu": "A"}], cfg)


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.headers = {}
        self.auth = None
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        payload = self.payloads.pop(0)
        response = requests.Response()
        response.status_code = 200
        response.headers = {"Content-Type": "application/json"}
        response._content = json.dumps(payload).encode("utf-8")
        response.url = url
        return response


class PaginationIntegrityTests(unittest.TestCase):
    def test_total_count_controls_pagination_and_is_verified(self):
        session = FakeSession(
            [
                {"status": True, "data": [{"id": 1}, {"id": 2}], "total": 3},
                {"status": True, "data": [{"id": 3}], "total": 3},
            ]
        )
        client = MobiWorkClient(
            "user",
            "token",
            min_interval_seconds=0,
            max_retries=0,
            session=session,
        )
        cfg = ReportConfig(
            key="bill",
            enabled=True,
            name="Bill",
            folder="Bill",
            url="https://example.test/bill",
            page_param="page_number",
            page_size_param="page_size",
            page_size=2,
            data_path="data",
            total_path="total",
        )

        records = client.fetch_report_range(cfg, date(2026, 8, 1), date(2026, 8, 1))

        self.assertEqual([row["id"] for row in records], [1, 2, 3])
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0][1]["page_number"], 1)
        self.assertEqual(session.calls[1][1]["page_number"], 2)

    def test_incomplete_dataset_is_rejected(self):
        session = FakeSession(
            [{"status": True, "data": [{"id": 1}], "total": 2}]
        )
        client = MobiWorkClient(
            "user",
            "token",
            min_interval_seconds=0,
            max_retries=0,
            session=session,
        )
        cfg = ReportConfig(
            key="bill",
            enabled=True,
            name="Bill",
            folder="Bill",
            url="https://example.test/bill",
            page_param="page_number",
            page_size_param="page_size",
            page_size=100,
            data_path="data",
            total_path="total",
        )

        with self.assertRaisesRegex(RuntimeError, "Refusing to export an incomplete dataset"):
            client.fetch_report_range(cfg, date(2026, 8, 1), date(2026, 8, 1))


if __name__ == "__main__":
    unittest.main()
