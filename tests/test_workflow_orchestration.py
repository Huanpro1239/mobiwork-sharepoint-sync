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
        rebuild = self._read("mobiwork-rebuild-month.yml")
        bootstrap = self._read("mobiwork-bootstrap-history.yml")
        history = self._read("historical-reconcile.yml")

        for workflow in (report, rebuild, bootstrap, history):
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

    def test_image_copy_removed_but_data_cham_anh_kept(self):
        existing = {path.name for path in WORKFLOWS.glob("*.yml")}
        self.assertNotIn("mobiwork-images.yml", existing)
        self.assertIn("data-cham-anh-backfill.yml", existing)

        removed_modules = {
            "image_sync.py",
            "image_sync_reliable.py",
            "image_storage.py",
            "run_images.py",
            "sharepoint_image_source.py",
        }
        present = {path.name for path in (ROOT / "src").glob("*.py")}
        self.assertTrue(removed_modules.isdisjoint(present))
        self.assertIn("data_cham_anh_export.py", present)

        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertNotIn("mobiwork-images.yml", text)
                self.assertNotIn("run_images.py", text)
                self.assertNotIn("SMOKE_CHECK_IMAGES", text)
                self.assertNotIn("IMAGE_REPAIR", text)

        report = self._read("mobiwork-sync.yml")
        rebuild = self._read("mobiwork-rebuild-month.yml")
        self.assertIn("run: python src/run_data_cham_anh.py", report)
        self.assertIn("run: python src/run_data_cham_anh_backfill.py", rebuild)
    def test_report_sync_runs_on_business_hours_schedule(self):
        report = self._read("mobiwork-sync.yml")

        self.assertIn('cron: "5 7-19/3 * * 1-6"', report)
        self.assertIn('if [ "$EVENT_SCHEDULE" = "5 7-19/3 * * 1-6" ]', report)
        self.assertIn('cron: "0 9 * * *"', report)
        self.assertNotIn('cron: "5 * * * *"', report)
        self.assertIn("DATA_CHAM_ANH_SKIP_WHEN_UNCHANGED", report)

    def test_report_sync_refreshes_promotion_bonus_snapshot(self):
        report = self._read("mobiwork-sync.yml")

        self.assertIn("run: python src/promotion_bonus.py", report)
        self.assertIn("output/promotion_bonus_manifest.json", report)
        self.assertIn("group: mobiwork-sharepoint-production", report)

    def test_production_sync_preflight_is_lightweight(self):
        report = self._read("mobiwork-sync.yml")

        self.assertIn("python -m compileall -q src", report)
        self.assertNotIn("python -m unittest", report)

    def test_nightly_reconciliation_defaults_to_three_completed_days(self):
        nightly = self._read("nightly-reconcile.yml")

        self.assertIn('default: "3"', nightly)
        self.assertIn('days="${INPUT_LOOKBACK:-3}"', nightly)
        self.assertIn('days="3"', nightly)
        self.assertIn('cron: "30 23 * * *"', nightly)

    def test_operations_health_runs_daily(self):
        health = self._read("operations-health.yml")

        self.assertIn('cron: "20 8 * * *"', health)
        self.assertIn("SYNC_STALE_MINUTES = 720", health)

    def test_recovery_rebuild_covers_current_month_and_month_close(self):
        recovery = self._read("recovery-rebuild.yml")

        self.assertIn('cron: "0 2 * * 0"', recovery)
        self.assertNotIn('cron: "0 5 * * 0"', recovery)
        self.assertIn('cron: "30 3 2 * *"', recovery)
        self.assertIn('previous_month_schedules = {"30 3 2 * *"}', recovery)
        self.assertIn('actions/workflows/mobiwork-rebuild-month.yml/dispatches', recovery)
        self.assertIn('dry_run:"false"', recovery)

    def test_monthly_history_reconcile_rescans_all_completed_history(self):
        history = self._read("historical-reconcile.yml")

        self.assertNotIn("\n  schedule:\n", history)
        self.assertIn("workflow_dispatch:", history)
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

    def test_operations_health_alerts_on_failed_or_missing_full_month_rebuild(self):
        health = self._read("operations-health.yml")

        self.assertIn("actions/workflows/mobiwork-rebuild-month.yml/runs", health)
        self.assertIn("REBUILD_STALE_MINUTES = 8 * 24 * 60", health)
        self.assertIn("Latest MobiWork Full Month Rebuild did not succeed", health)
        self.assertIn("rebuild_conclusion=", health)

    def test_sharepoint_writers_share_target_config_and_refreshing_oidc(self):
        writers = (
            "mobiwork-sync.yml",
            "mobiwork-rebuild-month.yml",
            "mobiwork-bootstrap-history.yml",
            "historical-reconcile.yml",
            "data-cham-anh-backfill.yml",
            "production-smoke.yml",
        )
        for name in writers:
            workflow = self._read(name)
            with self.subTest(workflow=name):
                self.assertIn("uses: ./.github/actions/resolve-sharepoint-drive", workflow)
                self.assertIn("vars.SHAREPOINT_HOST ||", workflow)
                self.assertIn("AZURE_CLIENT_ID: ${{ secrets.AZURE_CLIENT_ID }}", workflow)
                self.assertIn("AZURE_TENANT_ID: ${{ secrets.AZURE_TENANT_ID }}", workflow)
                self.assertIn("id-token: write", workflow)
                self.assertNotIn("sites/vikodacomvn.sharepoint.com:/sites/Planning", workflow)

    def test_third_party_actions_are_pinned_to_commit_sha(self):
        import re

        pattern = re.compile(r"uses:\s*([\w.-]+/[\w./-]+)@(\S+)")
        for path in sorted(WORKFLOWS.glob("*.yml")):
            for action, ref in pattern.findall(path.read_text(encoding="utf-8")):
                with self.subTest(workflow=path.name, action=action):
                    self.assertRegex(ref, r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()

class PromotionBonusWorkflowTests(unittest.TestCase):
    def test_bonus_failure_and_dry_run_artifacts(self):
        text = (WORKFLOWS / 'mobiwork-sync.yml').read_text(encoding='utf-8')
        bonus = text.split('      - name: Sync Promotion Bonus current snapshot')[1].split('      - name: Publish run summary')[0]
        self.assertNotIn('continue-on-error', bonus)
        self.assertIn("inputs.report_scope == 'all_reports'", bonus)
        shared = text.split('\nenv:\n')[1].split('\njobs:')[0]
        self.assertIn('DRY_RUN:', shared)
        self.assertIn('MOBIWORK_TOKEN:', shared)
        self.assertNotIn('MOBIWORK_TOKEN:', bonus)
        self.assertEqual(text.count('uses: azure/login@'), 1)
        self.assertLess(text.index('run: python src/run_all_reports.py'), text.index('run: python src/promotion_bonus.py'))
        self.assertLess(text.index('run: python src/run_data_cham_anh.py'), text.index('run: python src/promotion_bonus.py'))
        self.assertIn('path: output/*.xlsx', text)
        self.assertIn('output/promotion_bonus_manifest.json', text)

    def test_summary_executes_for_success_failure_and_skipped(self):
        import json
        import os
        import tempfile
        import textwrap
        from unittest.mock import patch
        text = (WORKFLOWS / 'mobiwork-sync.yml').read_text(encoding='utf-8')
        code = textwrap.dedent(text.split("          python - <<'PY'\n          import json\n          import os\n")[1].split('          PY')[0])
        code = 'import json\nimport os\n' + code
        # Redirect only manifest reads; execute the exact embedded summary script.
        original_path = Path
        for status in ('success', 'failed', None):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'output').mkdir()
                if status:
                    (root / 'output/promotion_bonus_manifest.json').write_text(
                        json.dumps(dict(status=status, program_count=3, data_row_count=7,
                                        dry_run=True, error='report incomplete' if status == 'failed' else '')),
                        encoding='utf-8')
                summary = root / 'summary.md'
                # Replace the pathlib import with the temporary filesystem mapping.
                script = code.replace('from pathlib import Path', '')
                with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary),
                                             'PROMOTION_BONUS_OUTCOME': 'skipped'}):
                    exec(compile(script, '<workflow summary>', 'exec'),
                         {'Path': lambda value, root=root: original_path(root / value)})
                content = summary.read_text(encoding='utf-8')
                self.assertIn('Promotion Bonus Snapshot', content)
                self.assertIn(status or 'skipped', content)
                for label in ('Programs', 'Data rows', 'Target rows', 'Reward rows', 'Workbook bytes', 'Dry run'):
                    self.assertIn(label, content)
                if status == 'failed':
                    self.assertIn('report incomplete', content)

class PromotionDetailWorkflowTests(unittest.TestCase):
    def test_detail_build_runs_after_sources_and_preserves_snapshot(self):
        text = (WORKFLOWS / 'mobiwork-sync.yml').read_text(encoding='utf-8')
        self.assertLess(text.index('run: python src/run_all_reports.py'), text.index('run: python src/promotion_detail.py'))
        self.assertLess(text.index('run: python src/promotion_bonus.py'), text.index('run: python src/promotion_detail.py'))
        self.assertIn('output/promotion_detail_manifest.json', text)
        self.assertIn('## Promotion detail report', text)
        self.assertIn('path: output/*.xlsx', text)
        import json
        cfg = json.loads((ROOT / 'config/promotion_detail.json').read_text(encoding='utf-8'))
        self.assertIs(cfg['publish_enabled'], True)
        self.assertIs(cfg['allow_incomplete_publish'], True)
        self.assertNotIn('publish_promotion_detail:', text)
        self.assertNotIn('allow_incomplete_detail:', text)
