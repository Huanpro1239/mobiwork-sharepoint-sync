from __future__ import annotations

import unittest

from scoring.prebuilt_classifier import _implementation_is_compatible


class PrebuiltCompatibilityTests(unittest.TestCase):
    def test_accepts_current_implementation_hash(self):
        self.assertTrue(
            _implementation_is_compatible(
                "current", "current", pipeline_version="2.3.0", schema_version=4
            )
        )

    def test_accepts_only_known_policy_contract_upgrade_from_production_bundle(self):
        previous = "bf00cf1d94b0bb1070a88671e95e11f55f998bad9c1c30ca82b6cdcb842d6331"
        current = "2589b464d36660e6bc66de6f3fc87cdef6bc3c66b8718fcd19f4bbad4e6ef01e"

        self.assertTrue(
            _implementation_is_compatible(
                previous, current, pipeline_version="2.3.0", schema_version=4
            )
        )
        self.assertFalse(
            _implementation_is_compatible(
                previous, current, pipeline_version="2.3.1", schema_version=4
            )
        )
        self.assertFalse(
            _implementation_is_compatible(
                previous, current, pipeline_version="2.3.0", schema_version=5
            )
        )
        self.assertFalse(
            _implementation_is_compatible(
                previous, "future-change", pipeline_version="2.3.0", schema_version=4
            )
        )

    def test_rejects_unknown_implementation_hash(self):
        self.assertFalse(
            _implementation_is_compatible(
                "unknown", "current", pipeline_version="2.3.0", schema_version=4
            )
        )


if __name__ == "__main__":
    unittest.main()
