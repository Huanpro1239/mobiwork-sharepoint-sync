import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pipeline  # noqa: E402
from report_context import CatalogueCache  # noqa: E402
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
        self.caches = []
        for name, module in (("reports", pipeline.run_all_reports), ("photos", pipeline.run_data_cham_anh),
                             ("promotion_bonus", pipeline.promotion_bonus),
                             ("promotion_detail", pipeline.promotion_detail)):
            def runner(*args, name=name):
                self.calls.append(name)
                self.caches.extend(args)
                return [{"status": "success"}] if name == "photos" else {"status": "success"}
            mock = patch.object(module, "run", side_effect=runner)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)

    def test_routine_calls_legacy_runners_once_in_order_with_shared_context(self):
        result = pipeline.run()
        self.assertEqual(self.calls, list(pipeline.SCOPES["all_reports"]))
        self.assertEqual(len(self.caches), 2)
        self.assertIs(self.caches[0], self.caches[1])
        self.assertEqual(json.loads(self.manifest.read_text())["status"], "success")
        self.assertFalse(result["needs_review"])
        pipeline.run()
        self.assertIsNot(self.caches[0], self.caches[2])

    def test_scopes_exclude_unrequested_writes(self):
        for scope in ("promotion_reports", "promotion_history", "promotion_bonus_only"):
            with self.subTest(scope=scope):
                self.calls.clear()
                pipeline.run(scope)
                self.assertEqual(self.calls, list(pipeline.SCOPES[scope]))

    def test_current_promotion_rebuild_shares_context_and_preserves_current_month_settings(self):
        with patch.dict(os.environ, {"PROMOTION_BONUS_MONTHS": "", "PROMOTION_DETAIL_MONTHS": "",
                                     "PROMOTION_DETAIL_SCOPE": "touched"}):
            result = pipeline.run("promotion_reports")
            self.assertEqual(os.environ["PROMOTION_BONUS_MONTHS"], "")
            self.assertEqual(os.environ["PROMOTION_DETAIL_SCOPE"], "touched")
        self.reports.assert_not_called()
        self.photos.assert_not_called()
        self.assertEqual(self.calls, ["promotion_bonus", "promotion_detail"])
        self.assertIs(self.caches[0], self.caches[1])
        self.assertEqual(result["status"], "success")

    def test_exception_stops_all_dependent_stages(self):
        self.photos.side_effect = RuntimeError("Image master invalid")
        with self.assertRaisesRegex(RuntimeError, "Image master invalid"):
            pipeline.run()
        self.promotion_bonus.assert_not_called()
        self.promotion_detail.assert_not_called()
        stages = json.loads(self.manifest.read_text())["stages"]
        self.assertEqual([stage["status"] for stage in stages], ["success", "failed", "skipped", "skipped"])

    def test_bad_return_stops_publication_even_without_exception(self):
        for result in ({"status": "partial_failure"}, {"status": "success", "failed_report_count": 1},
                       None, {"status": "running"}):
            with self.subTest(result=result):
                self.reports.side_effect = None
                self.reports.return_value = result
                with self.assertRaises(RuntimeError):
                    pipeline.run()
                self.photos.assert_not_called()
                self.promotion_bonus.assert_not_called()

    def test_partial_bonus_upload_does_not_appear_successful(self):
        self.promotion_bonus.side_effect = None
        self.promotion_bonus.return_value = {"status": "success", "publish_failures": ["file locked"]}
        with self.assertRaisesRegex(RuntimeError, "incomplete publication"):
            pipeline.run("promotion_history")
        self.promotion_detail.assert_not_called()

    def test_business_gaps_are_visible_without_hiding_successful_export(self):
        self.promotion_bonus.side_effect = None
        self.promotion_bonus.return_value = {"status": "success", "quality_status": "needs_review",
                                            "customer_catalogue": {"private": "must not be copied"}}
        result = pipeline.run()
        self.assertTrue(result["needs_review"])
        self.assertNotIn("private", self.manifest.read_text())

    def test_dry_run_preserves_env_for_each_runner(self):
        self.photos.side_effect = lambda: [] if env_bool("DRY_RUN") else self.fail("Dry run lost")
        with patch.dict(os.environ, {"DRY_RUN": "true"}):
            result = pipeline.run()
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["stages"][1]["status"], "skipped")

    def test_history_months_are_passed_to_both_runners_unchanged(self):
        def bonus(cache):
            self.assertEqual(os.environ["PROMOTION_BONUS_MONTHS"], "2026-09")
            return {"status": "success"}
        def detail(cache):
            self.assertEqual(os.environ["PROMOTION_DETAIL_MONTHS"], "2026-09")
            return {"status": "success"}
        self.promotion_bonus.side_effect = bonus
        self.promotion_detail.side_effect = detail
        with patch.dict(os.environ, {"PROMOTION_BONUS_MONTHS": "2026-09", "PROMOTION_DETAIL_MONTHS": "2026-09"}):
            pipeline.run("promotion_history")

    def test_history_defaults_and_single_month_selection_work_without_workflow(self):
        for supplied, expected in (("", "all_existing"), ("2026-09", "2026-09")):
            def runner(cache, expected=expected):
                self.assertEqual(os.environ["PROMOTION_BONUS_MONTHS"], expected)
                self.assertEqual(os.environ["PROMOTION_DETAIL_MONTHS"], expected)
                self.assertEqual(os.environ["PROMOTION_DETAIL_SCOPE"], "all_existing")
                return {"status": "success"}
            self.promotion_bonus.side_effect = runner
            self.promotion_detail.side_effect = runner
            with patch.dict(os.environ, {"PROMOTION_BONUS_MONTHS": supplied, "PROMOTION_DETAIL_MONTHS": "",
                                         "PROMOTION_DETAIL_SCOPE": "touched"}):
                pipeline.run("promotion_history")
                self.assertEqual(os.environ["PROMOTION_DETAIL_MONTHS"], "")
                self.assertEqual(os.environ["PROMOTION_DETAIL_SCOPE"], "touched")

    def test_inconsistent_history_months_fail_before_any_source_or_write(self):
        with (patch.dict(os.environ, {"PROMOTION_BONUS_MONTHS": "2026-09", "PROMOTION_DETAIL_MONTHS": "2026-08"}),
              self.assertRaisesRegex(ValueError, "same months")):
            pipeline.run("promotion_history")
        self.promotion_bonus.assert_not_called()
        self.assertFalse(self.manifest.exists())

    def test_context_records_one_catalogue_load_across_both_report_stages(self):
        loader = Mock(return_value={"products": {"SKU": {"Package": "Vật phẩm"}}})
        def runner(cache):
            cfg = cache.enrich("products", {}, loader)
            cfg["products"]["SKU"]["Package"] = "Local change"
            return {"status": "success"}
        self.promotion_bonus.side_effect = runner
        self.promotion_detail.side_effect = runner
        with patch("report_context.MobiWorkClient.from_env", return_value=Mock()):
            result = pipeline.run("promotion_history")
        self.assertEqual(result["catalogue_loads"], {"products": 1})
        loader.assert_called_once()

    def test_photo_return_without_status_cannot_pass(self):
        self.photos.side_effect = None
        self.photos.return_value = [{}]
        with self.assertRaisesRegex(RuntimeError, "Photo report"):
            pipeline.run()
        self.promotion_bonus.assert_not_called()

    def test_cli_preflight_requires_no_secrets_or_output_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if not k.startswith(("MOBIWORK_", "AZURE_", "PROMOTION_"))}
            result = subprocess.run([sys.executable, str(ROOT / "src/pipeline.py"), "--check"],
                                    cwd=tmp, env=env, capture_output=True, text=True, check=True)
            self.assertIn("template_columns", result.stdout)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_shared_modules_do_not_import_report_runners(self):
        runners = {"pipeline", "promotion_bonus", "promotion_detail", "promotion_bonus_ui"}
        for name in ("api_contract", "report_context", "report_runtime", "promotion_catalogue", "promotion_models",
                     "excel_export", "customer_catalogue", "promotion_workbook", "promotion_bonus_calc"):
            tree = ast.parse((ROOT / "src" / f"{name}.py").read_text(encoding="utf-8"))
            imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
            imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
            # Calculation uses the detail domain to resolve source units/sales,
            # while detail never imports the calculation runner.
            forbidden = runners - {"promotion_detail"} if name == "promotion_bonus_calc" else runners
            self.assertFalse(imports & forbidden, (name, imports & forbidden))


class CatalogueContextTests(unittest.TestCase):
    def test_successful_snapshot_is_read_once_and_each_consumer_gets_its_own_copy(self):
        cache = CatalogueCache(Mock())
        loader = Mock(return_value={"customer_catalogue": {"ID": {"name": "Original"}},
                                    "customer_catalogue_audit": {"count": 1}})
        bonus = cache.enrich("customers", {"purpose": "bonus"}, loader)
        bonus["customer_catalogue"]["ID"]["name"] = "Changed"
        detail = cache.enrich("customers", {"purpose": "detail", "customer_catalogue_start_date": "01/01/1900"}, loader)
        self.assertEqual(detail["customer_catalogue"]["ID"]["name"], "Original")
        self.assertEqual(detail["purpose"], "detail")
        self.assertEqual(cache.loads, {"customers": 1})
        loader.assert_called_once()

    def test_failed_reads_are_retried_and_different_inputs_do_not_reuse_mappings(self):
        cache = CatalogueCache(Mock())
        loader = Mock(side_effect=[ValueError("total changed"), {"customer_catalogue": {}}, {"customer_catalogue": {}}])
        with self.assertRaises(ValueError):
            cache.enrich("customers", {}, loader)
        cache.enrich("customers", {}, loader)
        cache.enrich("customers", {"customer_address_provinces": {"Province": "Province"}}, loader)
        self.assertEqual(loader.call_count, 3)
        self.assertEqual(cache.loads, {"customers": 2})

    def test_real_bonus_consumer_shares_customer_snapshot_with_detail(self):
        cache = CatalogueCache(Mock())
        loader = Mock(return_value={"customer_catalogue": {"ID": {"name": "Customer"}},
                                    "customer_catalogue_audit": {"count": 1}})
        with patch("customer_catalogue.enrich_customer_config", loader):
            customers, _ = pipeline.promotion_bonus._load_customers(cache.client, {}, {}, cache)
        detail = cache.enrich("customers", {}, loader)
        self.assertEqual(customers, detail["customer_catalogue"])
        loader.assert_called_once()


class RuntimeTests(unittest.TestCase):
    def test_shared_writer_preserves_base_template_and_new_order_reward_columns(self):
        from datetime import date

        import pandas as pd
        from openpyxl import load_workbook

        from promotion_workbook import write_detail_workbook

        columns = pipeline.promotion_detail.COLUMNS + list(pipeline.promotion_detail.BONUS_COLUMNS)
        row = dict.fromkeys(columns)
        row.update({"Mã Đơn hàng": "ORDER1", "THÀNH TIỀN": 120000, "Số lượng Khuyến mãi": 0,
                    "Thưởng phân bổ theo đơn": "Quà: 6", "Tiền thưởng phân bổ (đ)": 30000})
        with tempfile.TemporaryDirectory() as tmp:
            path = write_detail_workbook({"BaoCao": pd.DataFrame([row], columns=columns)},
                                         "report.xlsx", date(2026, 9, 1), Path(tmp))
            workbook = load_workbook(path)
            try:
                sheet = workbook["BaoCao"]
                self.assertEqual(sheet["W5"].value, 120000)
                self.assertEqual(sheet["Z5"].value, 0)
                self.assertEqual(sheet["AA5"].value, "Quà: 6")
                self.assertEqual(sheet["AB5"].value, 30000)
                self.assertEqual(sheet["AB5"].number_format, "#,##0")
                self.assertEqual(sheet.auto_filter.ref, "A4:AE5")
                self.assertIn("$AE$5", str(sheet.print_area))
            finally:
                workbook.close()

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
