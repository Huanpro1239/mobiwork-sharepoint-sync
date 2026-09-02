from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "mobiwork-images.yml"


class ImageWorkflowAutomationTests(unittest.TestCase):
    def test_production_allows_trusted_redirect_host(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('IMAGE_ALLOWED_HOSTS: "dmsimages.mobiwork.vn,image2.mobiwork.vn"', text)

    def test_catchup_handles_partial_failure_with_progress(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        continuation = text.split("- name: Continue image catch-up", maxsplit=1)[1]
        self.assertIn('[ "$status" != "partial_failure" ]', continuation)
        self.assertIn('[ "$uploaded" -le 0 ]', continuation)

    def test_complete_sync_dispatches_kpi_with_safe_dry_run_gate(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        trigger = text.split("- name: Trigger KPI after complete image sync", maxsplit=1)[1]
        self.assertIn('if has("dry_run") then (.dry_run | tostring)', trigger)
        self.assertIn("pending == 0", trigger)
        self.assertIn("image-scoring-kpi.yml/dispatches", trigger)


if __name__ == "__main__":
    unittest.main()
