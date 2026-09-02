from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "image-scoring-kpi.yml"
PROBE_WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cloud-kpi-main-probe.yml"
)


class KPIWorkflowAutomationTests(unittest.TestCase):
    def test_catchup_continues_by_pending_queue_not_successful_download_count(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        continuation = text.split("- name: Continue production catch-up", maxsplit=1)[1]
        continuation = continuation.split("- name: Upload AI/KPI artifacts", maxsplit=1)[0]

        self.assertIn("production_pending_remaining_unique", continuation)
        self.assertNotIn("loaded=", continuation)
        self.assertIn('"$pending" -gt 0', continuation)

    def test_workflow_sets_a_finite_technical_retry_limit(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("AI_PRODUCTION_MAX_TECHNICAL_RETRIES", text)

    def test_dry_run_gate_preserves_explicit_false(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn('if has("dry_run") then (.dry_run | tostring)', text)
        self.assertNotIn(".dry_run // true", text)

    def test_workflows_do_not_require_obsolete_legacy_ai_export(self):
        production = WORKFLOW.read_text(encoding="utf-8")
        probe = PROBE_WORKFLOW.read_text(encoding="utf-8")

        self.assertNotIn("AI_LEGACY_SCORE_REMOTE", production)
        self.assertNotIn("AI_LEGACY_SCORE_REMOTE", probe)


if __name__ == "__main__":
    unittest.main()
