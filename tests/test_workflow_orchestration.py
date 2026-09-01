from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class WorkflowOrchestrationTests(unittest.TestCase):
    @staticmethod
    def _read(name: str) -> str:
        return (WORKFLOWS / name).read_text(encoding="utf-8")

    def test_shared_production_queue_preserves_pending_runs(self):
        report = self._read("mobiwork-sync.yml")
        images = self._read("mobiwork-images.yml")

        for workflow in (report, images):
            self.assertIn("group: mobiwork-sharepoint-production", workflow)
            self.assertIn("cancel-in-progress: false", workflow)
            self.assertIn("queue: max", workflow)

    def test_daily_images_are_dispatched_after_finalized_report_refresh(self):
        report = self._read("mobiwork-sync.yml")
        images = self._read("mobiwork-images.yml")

        self.assertIn("Queue daily image sync after finalized report refresh", report)
        self.assertIn("env.SYNC_SCOPE == 'yesterday'", report)
        self.assertIn("actions/workflows/mobiwork-images.yml/dispatches", report)
        self.assertNotIn("\n  schedule:\n", images)

    def test_kpi_is_not_triggered_by_generic_workflow_completion(self):
        kpi = self._read("image-scoring-kpi.yml")

        self.assertNotIn("workflow_run:", kpi)
        self.assertIn("workflow_dispatch:", kpi)

    def test_image_workflow_dispatches_kpi_only_after_complete_production_run(self):
        images = self._read("mobiwork-images.yml")

        self.assertIn("Trigger KPI after complete image sync", images)
        self.assertIn("status=$(jq -r '.status // \"unknown\"' \"$path\")", images)
        self.assertIn("dry_run=$(jq -r '.dry_run // true' \"$path\")", images)
        self.assertIn('[ "$status" != "success" ]', images)
        self.assertIn('[ "$dry_run" != "false" ]', images)
        self.assertIn('[ "$pending" -ne 0 ]', images)
        self.assertIn('[ "$failed" -ne 0 ]', images)
        self.assertIn("actions/workflows/image-scoring-kpi.yml/dispatches", images)


if __name__ == "__main__":
    unittest.main()
