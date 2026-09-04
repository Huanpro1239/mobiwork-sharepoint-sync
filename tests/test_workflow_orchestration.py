from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class WorkflowOrchestrationTests(unittest.TestCase):
    @staticmethod
    def _read(name: str) -> str:
        return (WORKFLOWS / name).read_text(encoding="utf-8")

    def test_shared_production_lock_never_interrupts_active_writer(self):
        report = self._read("mobiwork-sync.yml")
        images = self._read("mobiwork-images.yml")
        rebuild = self._read("mobiwork-rebuild-month.yml")
        bootstrap = self._read("mobiwork-bootstrap-history.yml")
        history = self._read("historical-reconcile.yml")

        for workflow in (report, images, rebuild, bootstrap, history):
            self.assertIn("group: mobiwork-sharepoint-production", workflow)
            self.assertIn("cancel-in-progress: false", workflow)
            self.assertNotIn("queue: max", workflow)

    def test_bootstrap_pauses_routines_but_keeps_manual_recovery_available(self):
        bootstrap = self._read("mobiwork-bootstrap-history.yml")

        self.assertIn("workflow_dispatch:", bootstrap)
        self.assertNotIn("\n  schedule:\n", bootstrap)
        self.assertIn('default: "2026-06"', bootstrap)
        self.assertIn("timeout-minutes: 360", bootstrap)
        self.assertIn("actions: write", bootstrap)
        self.assertIn("Pause routine production workflows", bootstrap)
        self.assertIn("Resume routine production workflows", bootstrap)
        self.assertIn("success() && inputs.dry_run == false", bootstrap)
        self.assertIn("run: python src/bootstrap_history.py", bootstrap)
        self.assertIn("group: mobiwork-sharepoint-production", bootstrap)
        self.assertIn("cancel-in-progress: false", bootstrap)
        self.assertIn('test_mobiwork.py', bootstrap)
        self.assertIn('test_region_mapping.py', bootstrap)
        self.assertIn('test_monthly_master.py', bootstrap)
        self.assertIn('EMPLOYEE_REGION_STRICT: "false"', bootstrap)
        self.assertIn(
            "Manual mobiwork-rebuild-month.yml remains enabled for recovery.",
            bootstrap,
        )

        routine_workflows = (
            "mobiwork-sync.yml",
            "mobiwork-images.yml",
            "nightly-reconcile.yml",
            "recovery-rebuild.yml",
            "historical-reconcile.yml",
            "production-smoke.yml",
            "operations-health.yml",
        )
        for name in routine_workflows:
            self.assertGreaterEqual(bootstrap.count(name), 2)
        self.assertIn("/disable", bootstrap)
        self.assertIn("/enable", bootstrap)

    def test_bootstrap_repairs_legacy_disabled_manual_rebuild_state(self):
        bootstrap = self._read("mobiwork-bootstrap-history.yml")
        enable_path = "actions/workflows/mobiwork-rebuild-month.yml/enable"

        self.assertGreaterEqual(bootstrap.count(enable_path), 2)
        self.assertIn(
            "Ensured mobiwork-rebuild-month.yml is enabled for recovery.",
            bootstrap,
        )
        self.assertIn(
            "Confirmed mobiwork-rebuild-month.yml is enabled.",
            bootstrap,
        )

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

    def test_production_sync_preflight_covers_source_and_merge_integrity(self):
        report = self._read("mobiwork-sync.yml")

        self.assertIn('test_mobiwork.py', report)
        self.assertIn('test_monthly_master.py', report)

    def test_nightly_reconciliation_defaults_to_fourteen_completed_days(self):
        nightly = self._read("nightly-reconcile.yml")

        self.assertIn('default: "14"', nightly)
        self.assertIn('days="${INPUT_LOOKBACK:-14}"', nightly)
        self.assertIn('days="14"', nightly)
        self.assertIn('cron: "30 23 * * *"', nightly)

    def test_recovery_rebuild_covers_current_previous_and_month_close(self):
        recovery = self._read("recovery-rebuild.yml")

        self.assertIn('cron: "0 2 * * 0"', recovery)
        self.assertIn('cron: "0 5 * * 0"', recovery)
        self.assertIn('cron: "30 3 2 * *"', recovery)
        self.assertIn('previous_month_schedules = {"0 5 * * 0", "30 3 2 * *"}', recovery)
        self.assertIn('actions/workflows/mobiwork-rebuild-month.yml/dispatches', recovery)
        self.assertIn('dry_run:"false"', recovery)

    def test_monthly_history_reconcile_rescans_all_completed_history(self):
        history = self._read("historical-reconcile.yml")

        self.assertIn('cron: "30 4 3 * *"', history)
        self.assertIn('default: "2026-06"', history)
        self.assertIn("run: python src/reconcile_history.py", history)
        self.assertIn('test_reconcile_history.py', history)
        self.assertIn("cancel-in-progress: false", history)

    def test_rebuild_is_recovery_safe_before_bootstrap_is_complete(self):
        rebuild = self._read("mobiwork-rebuild-month.yml")

        self.assertIn('test_mobiwork.py', rebuild)
        self.assertIn('test_region_mapping.py', rebuild)
        self.assertIn('test_monthly_master.py', rebuild)
        self.assertIn('test_rebuild_month.py', rebuild)
        self.assertIn('BOOTSTRAP_BYPASS_GATE: "true"', rebuild)
        self.assertIn('EMPLOYEE_REGION_STRICT: "false"', rebuild)
        self.assertIn("cancel-in-progress: false", rebuild)

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
