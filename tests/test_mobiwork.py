import unittest

from src.mobiwork import expand_records, get_by_path


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


if __name__ == "__main__":
    unittest.main()
