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
        rebuild = self._read("mobiwork-rebuild-month.yml")

        for workflow in (report, images, rebuild):
            self.assertIn("group: mobiwork-sharepoint-production", workflow)
            self.assertNotIn("queue: max", workflow)

        self.assertIn("cancel-in-progress: false", report)
        self.assertIn("cancel-in-progress: false", images)
        self.assertIn("cancel-in-progress: true", rebuild)

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

    def test_nightly_reconciliation_defaults_to_seven_completed_days(self):
        nightly = self._read("nightly-reconcile.yml")

        self.assertIn('default: "7"', nightly)
        self.assertIn('days="${INPUT_LOOKBACK:-7}"', nightly)
        self.assertIn('days="7"', nightly)
        self.assertIn('cron: "30 23 * * *"', nightly)

    def test_recovery_rebuild_covers_weekly_current_month_and_month_close(self):
        recovery = self._read("recovery-rebuild.yml")

        self.assertIn('cron: "0 2 * * 0"', recovery)
        self.assertIn('cron: "30 2 1 * *"', recovery)
        self.assertIn('scope = "previous_month" if schedule == "30 2 1 * *"', recovery)
        self.assertIn('actions/workflows/mobiwork-rebuild-month.yml/dispatches', recovery)
        self.assertIn('dry_run:"false"', recovery)

    def test_removed_scoring_workflows_are_absent(self):
        removed = {
            "cloud-kpi-dryrun.yml",
            "cloud-kpi-main-probe.yml",
            "image-scoring-kpi.yml",
            "migrate-kpi-bundle-main.yml",
        }
        existing = {path.name for path in WORKFLOWS.glob("*.yml")}
        self.assertTrue(removed.isdisjoint(existing))

    def test_image_workflow_only_synchronizes_images(self):
        images = self._read("mobiwork-images.yml")

        self.assertIn("IMAGE_FAIL_ON_PARTIAL: \"false\"", images)
        self.assertIn("python src/run_images.py", images)
        self.assertNotIn("Trigger KPI", images)
        self.assertNotIn("image-scoring-kpi.yml", images)
        self.assertNotIn("IMAGE_KPI_", images)

    def test_partial_image_failure_can_continue_only_when_forward_progress_exists(self):
        images = self._read("mobiwork-images.yml")

        self.assertIn('[ "$status" != "warming_up" ] && [ "$status" != "partial_failure" ]', images)
        self.assertIn('[ "$uploaded" -le 0 ]', images)
        self.assertIn("keeping retry cursor for the next production pass", images)
        self.assertIn("actions/workflows/mobiwork-images.yml/dispatches", images)


if __name__ == "__main__":
    unittest.main()
