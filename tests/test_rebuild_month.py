import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import rebuild_month as rebuild  # noqa: E402


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 3, 10, 0, tzinfo=tz)


class ResolveAnchorTests(unittest.TestCase):
    def test_blank_target_uses_current_vietnam_date(self):
        with patch.object(rebuild, "datetime", FixedDateTime):
            self.assertEqual(rebuild.resolve_anchor(""), FixedDateTime(2026, 9, 3).date())

    def test_current_month_rebuilds_through_today_only(self):
        with patch.object(rebuild, "datetime", FixedDateTime):
            self.assertEqual(
                rebuild.resolve_anchor("2026-09"),
                FixedDateTime(2026, 9, 3).date(),
            )

    def test_past_month_rebuilds_through_month_end(self):
        with patch.object(rebuild, "datetime", FixedDateTime):
            self.assertEqual(
                rebuild.resolve_anchor("2026-08"),
                FixedDateTime(2026, 8, 31).date(),
            )

    def test_future_month_is_rejected(self):
        with (
            patch.object(rebuild, "datetime", FixedDateTime),
            self.assertRaisesRegex(ValueError, "future"),
        ):
            rebuild.resolve_anchor("2026-10")

    def test_invalid_month_format_is_rejected(self):
        with (
            patch.object(rebuild, "datetime", FixedDateTime),
            self.assertRaisesRegex(ValueError, "YYYY-MM"),
        ):
            rebuild.resolve_anchor("09/2026")


if __name__ == "__main__":
    unittest.main()
