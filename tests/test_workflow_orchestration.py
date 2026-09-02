from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class WorkflowOrchestrationTests(unittest.TestCase):
    @staticmethod
    def _read(name: str) -> str:
        return (WORKFLOWS / name).read_text(encoding="utf-8")

    def test_shared_production_lock_uses_supported_concurrency_keys(self):
        report = self._read("mobiwork-sync.yml")
        images = self._read("mobiwork-images.yml")

        for workflow in (report, images):
            self.assertIn("group: mobiwork-sharepoint-production", workflow)
            self.assertIn("cancel-in-progress: false", workflow)
            self.assertNotIn("queue: max", workflow)

    def test_images_are_dispatched_after_successful_production_report_refresh(self):
        report = self._read("mobiwork-sync.yml")
        images = self._read("mobiwork-images.yml")

        self.assertIn("Queue image sync after successful production report refresh", report)
        self.assertIn("env.SYNC_SCOPE == 'yesterday'", report)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.dry_run == false", report)
        self.assertIn("actions/workflows/mobiwork-images.yml/dispatches", report)
        self.assertNotIn("run: python src/run_images.py", report)
        self.assertNotIn("\n  schedule:\n", images)

    def test_manual_report_refresh_forces_image_window_for_same_scope(self):
        report = self._read("mobiwork-sync.yml")

        self.assertIn('if [ "$EVENT_NAME" = "workflow_dispatch" ]', report)
        self.assertIn('if scope == "today":', report)
        self.assertIn('elif scope == "yesterday":', report)
        self.assertIn('elif scope == "lookback":', report)
        self.assertIn('today - timedelta(days=lookback)', report)
        self.assertIn("--arg from_date \"$from_date\"", report)

    def test_kpi_is_not_triggered_by_generic_workflow_completion(self):
        kpi = self._read("image-scoring-kpi.yml")

        self.assertNotIn("workflow_run:", kpi)
        self.assertIn("workflow_dispatch:", kpi)

    def test_image_workflow_uses_controlled_degraded_kpi_gate(self):
        images = self._read("mobiwork-images.yml")

        self.assertIn("Trigger KPI after complete image sync", images)
        self.assertIn("IMAGE_FAIL_ON_PARTIAL: \"false\"", images)
        self.assertIn("IMAGE_KPI_MIN_COMPLETENESS_PCT: \"99.0\"", images)
        self.assertIn("IMAGE_KPI_MAX_FAILED_IMAGES: \"10\"", images)
        self.assertIn("status == \"partial_failure\"", images)
        self.assertIn("completeness >= min_completeness", images)
        self.assertIn("failed <= max_failed", images)
        self.assertIn("deferred == 0", images)
        self.assertIn("actions/workflows/image-scoring-kpi.yml/dispatches", images)

    def test_partial_image_failure_can_continue_only_when_forward_progress_exists(self):
        images = self._read("mobiwork-images.yml")

        self.assertIn('[ "$status" != "warming_up" ] && [ "$status" != "partial_failure" ]', images)
        self.assertIn('[ "$uploaded" -le 0 ]', images)
        self.assertIn("keeping retry cursor for the next production pass", images)
        self.assertIn("actions/workflows/mobiwork-images.yml/dispatches", images)


if __name__ == "__main__":
    unittest.main()
