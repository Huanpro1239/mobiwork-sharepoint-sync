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

    def test_image_sync_never_dispatches_removed_scoring_pipeline(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("Trigger KPI", text)
        self.assertNotIn("image-scoring-kpi.yml", text)
        self.assertNotIn("IMAGE_KPI_", text)


if __name__ == "__main__":
    unittest.main()
