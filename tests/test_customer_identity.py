from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from monthly_master import build_month_from_partitions  # noqa: E402


class CustomerIdentityTests(unittest.TestCase):
    def test_distinct_ids_with_same_makh_are_preserved(self):
        partitions = [
            (
                date(2026, 6, 29),
                [
                    {
                        "ID": "6a41f58f2b7b17c81c3dd4e7",
                        "makh": "HANC061331",
                        "tenkh": "siêu thị đức thành Xala",
                    },
                    {
                        "ID": "6a41f58f2b7b17c81c3dd506",
                        "makh": "HANC061331",
                        "tenkh": "Nhà Hàng Bếp Sạch",
                    },
                ],
            )
        ]

        master = build_month_from_partitions(partitions, "flat", upsert_keys=["ID"])
        frame = master["Data"]

        self.assertEqual(len(frame), 2)
        self.assertEqual(frame["makh"].tolist(), ["HANC061331", "HANC061331"])
        self.assertEqual(len(set(frame["ID"].tolist())), 2)

    def test_same_id_is_upserted_across_partitions(self):
        partitions = [
            (
                date(2026, 6, 1),
                [{"ID": "customer-1", "makh": "KH01", "tenkh": "Tên cũ"}],
            ),
            (
                date(2026, 6, 2),
                [{"ID": "customer-1", "makh": "KH01", "tenkh": "Tên mới"}],
            ),
        ]

        master = build_month_from_partitions(partitions, "flat", upsert_keys=["ID"])
        frame = master["Data"]

        self.assertEqual(len(frame), 1)
        self.assertEqual(frame["ID"].tolist(), ["customer-1"])
        self.assertEqual(frame["tenkh"].tolist(), ["Tên mới"])


if __name__ == "__main__":
    unittest.main()
