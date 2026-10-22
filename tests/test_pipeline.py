import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pipeline  # noqa: E402
from report_runtime import env_bool, write_manifest  # noqa: E402


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.manifest = Path(temporary.name) / "pipeline.json"
        self.patch = patch.object(pipeline, "MANIFEST_PATH", self.manifest)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.calls = []
        for name, module in (("reports", pipeline.run_all_reports), ("photos", pipeline.run_data_cham_anh)):
            def runner(name=name):
                self.calls.append(name)
                return [{"status": "success"}] if name == "photos" else {"status": "success"}
            mock = patch.object(module, "run", side_effect=runner)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)

    def test_routine_calls_report_runners_once_in_order(self):
        result = pipeline.run()
        self.assertEqual(self.calls, ["reports", "photos"])
        self.assertEqual(json.loads(self.manifest.read_text())["status"], "success")
        self.assertEqual(result["status"], "success")

    def test_only_the_data_pull_scope_exists(self):
        self.assertEqual(pipeline.SCOPES, {"all_reports": ("reports", "photos")})
        with self.assertRaisesRegex(ValueError, "Unknown report scope"):
            pipeline.run("promotion_reports")

    def test_exception_stops_dependent_stages(self):
        self.reports.side_effect = RuntimeError("Source invalid")
        with self.assertRaisesRegex(RuntimeError, "Source invalid"):
            pipeline.run()
        self.photos.assert_not_called()
        stages = json.loads(self.manifest.read_text())["stages"]
        self.assertEqual([stage["status"] for stage in stages], ["failed", "skipped"])

    def test_bad_return_stops_publication_even_without_exception(self):
        for result in ({"status": "partial_failure"}, {"status": "success", "failed_report_count": 1},
                       None, {"status": "running"}):
            with self.subTest(result=result):
                self.reports.side_effect = None
                self.reports.return_value = result
                with self.assertRaises(RuntimeError):
                    pipeline.run()
                self.photos.assert_not_called()

    def test_manifest_keeps_only_operational_metadata(self):
        self.reports.side_effect = None
        self.reports.return_value = {"status": "success", "successful_report_count": 4,
                                     "report_results": [{"private": "must not be copied"}]}
        result = pipeline.run()
        self.assertEqual(result["stages"][0]["successful_report_count"], 4)
        self.assertNotIn("private", self.manifest.read_text())

    def test_dry_run_preserves_env_for_each_runner(self):
        self.photos.side_effect = lambda: [] if env_bool("DRY_RUN") else self.fail("Dry run lost")
        with patch.dict(os.environ, {"DRY_RUN": "true"}):
            result = pipeline.run()
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["stages"][1]["status"], "skipped")

    def test_photo_return_without_status_cannot_pass(self):
        self.photos.side_effect = None
        self.photos.return_value = [{}]
        with self.assertRaisesRegex(RuntimeError, "Photo report"):
            pipeline.run()

    def test_cli_preflight_requires_no_secrets_or_output_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if not k.startswith(("MOBIWORK_", "AZURE_"))}
            result = subprocess.run([sys.executable, str(ROOT / "src/pipeline.py"), "--check"],
                                    cwd=tmp, env=env, capture_output=True, text=True, check=True)
            self.assertIn("all_reports", result.stdout)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_shared_modules_do_not_import_the_pipeline(self):
        for name in ("api_contract", "report_runtime", "excel_export", "order_units"):
            tree = ast.parse((ROOT / "src" / f"{name}.py").read_text(encoding="utf-8"))
            imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
            imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
            self.assertNotIn("pipeline", imports, name)


class RuntimeTests(unittest.TestCase):
    def test_atomic_manifest_keeps_previous_file_on_serialization_or_replace_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            write_manifest(path, {"status": "success"})
            previous = path.read_bytes()
            with self.assertRaises(TypeError):
                write_manifest(path, {"status": object()})
            with patch.object(Path, "replace", side_effect=OSError("locked")), self.assertRaises(OSError):
                write_manifest(path, {"status": "failed"})
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_boolean_flags_share_whitespace_and_truth_values(self):
        for value in ("1", " TRUE ", "yes", "on"):
            with patch.dict(os.environ, {"TASK_FLAG": value}):
                self.assertTrue(env_bool("TASK_FLAG"))
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(env_bool("TASK_FLAG", True))


if __name__ == "__main__":
    unittest.main()
