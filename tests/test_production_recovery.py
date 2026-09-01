import sys
import unittest
from datetime import date
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import production_recovery as recovery  # noqa: E402


class ProductionRecoveryPlanTests(unittest.TestCase):
    def test_repairable_report_mismatch_is_eligible(self):
        manifest = {
            "status": "failed",
            "target_date": "2026-09-01",
            "reports": [
                {
                    "report": "visit",
                    "status": "failed",
                    "failure_stage": "data_mismatch",
                    "repairable": True,
                }
            ],
            "image_state": {"status": "success"},
        }

        plan = recovery.build_recovery_plan(manifest, today=date(2026, 9, 2))

        self.assertTrue(plan["eligible"])
        self.assertTrue(plan["report_repair"])
        self.assertFalse(plan["image_repair"])
        self.assertEqual(plan["lookback_days"], 1)
        self.assertEqual(plan["repair_attempt_budget"], 1)

    def test_image_only_failure_is_eligible(self):
        manifest = {
            "status": "failed",
            "target_date": "2026-09-01",
            "reports": [{"report": "visit", "status": "success"}],
            "image_state": {
                "status": "failed",
                "failure_stage": "image_state_consistency",
                "repairable": True,
            },
        }

        plan = recovery.build_recovery_plan(manifest, today=date(2026, 9, 2))

        self.assertTrue(plan["eligible"])
        self.assertFalse(plan["report_repair"])
        self.assertTrue(plan["image_repair"])
        self.assertEqual(plan["from_date"], "2026-09-01")

    def test_source_failure_disables_automatic_repair(self):
        manifest = {
            "status": "failed",
            "target_date": "2026-09-01",
            "reports": [
                {
                    "report": "bill",
                    "status": "failed",
                    "failure_stage": "mobiwork_fetch",
                    "repairable": False,
                }
            ],
            "image_state": {"status": "success"},
        }

        plan = recovery.build_recovery_plan(manifest, today=date(2026, 9, 2))

        self.assertFalse(plan["eligible"])
        self.assertIn("not safe", plan["reason"])
        self.assertIn("report:bill:mobiwork_fetch", plan["unrepairable_failures"])

    def test_target_outside_window_is_not_repaired(self):
        manifest = {
            "status": "failed",
            "target_date": "2026-07-01",
            "reports": [
                {
                    "report": "visit",
                    "status": "failed",
                    "failure_stage": "data_mismatch",
                    "repairable": True,
                }
            ],
            "image_state": {"status": "success"},
        }

        plan = recovery.build_recovery_plan(manifest, today=date(2026, 9, 2))

        self.assertFalse(plan["eligible"])
        self.assertEqual(plan["lookback_days"], 0)
        self.assertIn("31-day", plan["reason"])

    def test_healthy_smoke_never_repairs(self):
        manifest = {
            "status": "success",
            "target_date": "2026-09-01",
            "reports": [{"report": "visit", "status": "success"}],
            "image_state": {"status": "success"},
        }

        plan = recovery.build_recovery_plan(manifest, today=date(2026, 9, 2))

        self.assertFalse(plan["eligible"])
        self.assertIn("not failed", plan["reason"])


if __name__ == "__main__":
    unittest.main()
