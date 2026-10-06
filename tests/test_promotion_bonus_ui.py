from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

import promotion_bonus as bonus
import promotion_bonus_ui as ui

ORG = "6a194952f7222eb44825f347"
P1 = "6abb7a8602a9b7700e9aee94"
P2 = "6abb7b1902a9b7700e9af06b"
WEB_ENV = {"MOBIWORK_WEB_EMAIL": "bot%40example.com", "MOBIWORK_WEB_TOKENKEY": "SECRET-TOKEN",
           "MOBIWORK_WEB_ALIAS": "vikoda"}


def response(payload, status=200):
    item = Mock(status_code=status)
    item.json.return_value = payload
    item.raise_for_status.side_effect = None if status < 400 else RuntimeError(status)
    return item


def final(rows, targets=(), rewards=()):
    return {"result": list(rows), "arrChiTieu": list(targets), "arrTraThuong": list(rewards),
            "arrFormElement": [], "message": ""}


ENVELOPE = {"result": [{"RecordType": "_Orders", "result": [{"data": {}}], "options": {}}],
            "arrChiTieu": [], "arrTraThuong": [], "message": ""}


class SessionAndRequestTests(unittest.TestCase):
    def test_session_from_env_builds_page_equivalent_headers_without_leaking_token(self):
        with patch.dict(os.environ, WEB_ENV, clear=False):
            self.assertTrue(ui.WebSession.configured())
            session = ui.WebSession.from_env()
        headers = session.headers()
        decoded = base64.b64decode(headers["Authorization"].split()[1]).decode()
        self.assertEqual(decoded, "bot@example.com:SECRET-TOKEN")
        self.assertEqual(headers["x-alias"], "vikoda")
        self.assertEqual(session.service_url, ui.DEFAULT_SERVICE_URL)
        self.assertNotIn("SECRET", repr(session))
        with self.assertRaises(ValueError):
            ui.WebSession("a", "b", "c", "http://insecure")
        with patch.dict(os.environ, {k: "" for k in WEB_ENV}):
            self.assertFalse(ui.WebSession.configured())

    def test_query_matches_captured_october_request_and_sttt_enum(self):
        params = ui.report_query(ORG, date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(params["startDate"], 1790787600000)
        self.assertEqual(params["endDate"], 1793379600000)
        self.assertNotIn("sttt", params)
        self.assertEqual(ui.report_query(ORG, date(2026, 10, 1), date(2026, 10, 31), "-1")["sttt"], "-1")
        for bad in ("3", "total"):
            with self.assertRaises(ValueError):
                ui.report_query(ORG, date(2026, 10, 1), date(2026, 10, 31), bad)
        with self.assertRaises(ValueError):
            ui.report_query("bad", date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(ui.report_period(today=date(2026, 2, 10)), (date(2026, 2, 1), date(2026, 2, 28)))

    def test_classify_final_empty_envelope_and_errors(self):
        self.assertEqual(ui.classify(final([{"ma": "KH1"}])), ui.FINAL)
        self.assertEqual(ui.classify(final([])), ui.EMPTY)
        self.assertEqual(ui.classify(ENVELOPE), ui.ENVELOPE)
        with self.assertRaises(RuntimeError):
            ui.classify({"result": [], "message": "Không có quyền"})
        with self.assertRaises(ValueError):
            ui.classify({"result": [ENVELOPE["result"][0], {"ma": "KH1"}], "message": ""})
        with self.assertRaises(ValueError):
            ui.classify({"message": ""})


class FetchTests(unittest.TestCase):
    session = ui.WebSession("bot@example.com", "SECRET-TOKEN", "vikoda")
    policy = ui.FetchPolicy(attempts=3, backoff_seconds=10, pause_seconds=2, timeout_seconds=30)

    def test_one_program_per_post_and_envelopes_are_retried(self):
        http = Mock()
        http.post.side_effect = [response(ENVELOPE), response(final([{"ma": "KH1"}])),
                                 response(final([]))]
        sleeps = []
        programs = [{"_id": P1, "orgid": ORG, "name": "CT1"}, {"_id": P2, "orgid": ORG, "name": "CT2"}]
        results = ui.fetch_ui_snapshot(programs, date(2026, 10, 1), date(2026, 10, 31),
                                       session=self.session, policy=self.policy, http=http,
                                       sleep=sleeps.append)
        self.assertEqual([r.status for r in results], [ui.FINAL, ui.EMPTY])
        self.assertEqual(results[0].attempts, 2)
        self.assertEqual(sleeps, [10, 2])
        for call, pid in zip(http.post.call_args_list, [P1, P1, P2], strict=True):
            self.assertEqual(call.kwargs["json"], {"arrCT": [pid]})
            self.assertTrue(call.args[0].endswith("/PromotionBonusReport"))
            self.assertEqual(call.kwargs["params"]["orgid"], ORG)

    def test_persistent_envelope_is_unresolved_not_empty(self):
        http = Mock()
        http.post.return_value = response(ENVELOPE)
        result = ui.fetch_program(http, self.session, {"_id": P1}, {}, self.policy, sleep=lambda s: None)
        self.assertEqual(result.status, ui.ENVELOPE)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(http.post.call_count, 3)
        counts = ui.snapshot_counts([result])
        self.assertEqual(counts["unresolved_programs"], [{"id": P1, "name": ""}])

    def test_rejected_session_raises_auth_error(self):
        http = Mock()
        http.post.return_value = response({}, status=401)
        with self.assertRaises(ui.WebAuthError):
            ui.fetch_program(http, self.session, {"_id": P1}, {}, self.policy, sleep=lambda s: None)

    def test_orgid_must_be_resolvable(self):
        with self.assertRaises(ValueError), patch.dict(os.environ, {"MOBIWORK_ORG_ID": ""}):
            ui.fetch_ui_snapshot([{"_id": P1, "orgid": ORG}, {"_id": P2, "orgid": "b" * 24}],
                                 date(2026, 10, 1), date(2026, 10, 31), session=self.session,
                                 policy=self.policy, http=Mock(), sleep=lambda s: None)


class MathsTests(unittest.TestCase):
    def test_range_rules_mirror_render_data(self):
        self.assertEqual(ui.evaluate_range(5_000_000, 10_000_000, 0), (5_000_000, 50))
        # Reaching min exactly is already 100% by the plain formula.
        self.assertEqual(ui.evaluate_range(10_000_000, 10_000_000, 0), (0, 100))
        self.assertEqual(ui.evaluate_range(10_000_001, 10_000_000, 0), (0, 100))
        self.assertEqual(ui.evaluate_range(7_500_000, 5_000_000, 7_500_000), (0, 0))
        self.assertEqual(ui.evaluate_range(5_000_000, 5_000_000, 7_500_000), (0, 100))
        self.assertEqual(ui.evaluate_range(1, 0, 0), (0, 100))
        self.assertEqual(ui.evaluate_range(0, 0, 0), (0, 0))
        self.assertEqual(ui.evaluate_threshold(6, 6), (0, 100))
        self.assertEqual(ui.evaluate_threshold(3, 6), (3, 50))

    def test_default_type_uses_target_id_and_program_match(self):
        target = {"_id": "t1", "_idCT": P1, "ten": "Alkaline", "kh": " >= 40", "min": 40, "max": 0}
        row = {"_idCT": P1, "type": "", "objThucHien": {"t1": 20}, "objChiTieu": {}}
        cell = ui.target_cells(row, [target])[0]
        self.assertEqual((cell["Kế hoạch"], cell["Thực hiện"], cell["Còn lại"], cell["Tỷ lệ (%)"]),
                         (" >= 40", 20, 20, 50))
        other = ui.target_cells({**row, "_idCT": P2}, [target])[0]
        self.assertEqual(other["Thực hiện"], 0)

    def test_so_tien_multi_product_and_group_branches(self):
        so_tien = [{"_id": "so_tien0", "_idCT": P1, "ten": "DS", "kh": ">= 10", "min": 10, "max": 0}]
        cell = ui.target_cells({"_idCT": P1, "objThucHien": {"so_tien0": 12}}, so_tien)[0]
        self.assertTrue(cell["Đạt"])

        multi = [{"_id": "m1", "ten": "Combo", "isSL": True,
                  "kh": [{"_id": "sp1", "ten": "500ml", "sl": 6}, {"_id": "sp2", "ten": "1.5L", "sl": 4}]}]
        row = {"type": "MUTI_SP_SL_SP", "objChiTieu": {"m1": {"ct": {}}},
               "objThucHien": {"m1": {"ct": {"sp1": 6, "sp2": 1}}}}
        cells = ui.target_cells(row, multi)
        self.assertEqual([(c["Hạng mục"], c["Còn lại"], c["Tỷ lệ (%)"]) for c in cells],
                         [("500ml", 0, 100), ("1.5L", 3, 25)])
        self.assertEqual(ui.target_cells({**row, "objChiTieu": {}}, multi), [])

        group = [{"_id": "g1", "_idCT": P1, "ten": "Nhóm", "sp": {"Nước": [], "Gas": []},
                  "sotien": {"Nước": 100, "Gas": 50}}]
        row = {"_idCT": P1, "type": "GR_ST_MIN_SP", "objThucHien": {"g1": {"Nước": 100, "Gas": 10}}}
        self.assertEqual([c["Tỷ lệ (%)"] for c in ui.target_cells(row, group)], [100, 20])


class WorkbookTests(unittest.TestCase):
    def results(self):
        targets = [{"_id": "t1", "_idCT": P1, "ten": "Alkaline", "kh": " >= 40", "min": 40, "max": 0}]
        rewards = [{"_id": "r1", "ten": "Nước 500ml (Thùng)"}]
        rows = [
            {"_idCT": P1, "ma": "KH1", "ten": "Quán A", "kv": "Miền Trung 1B", "npp": {"name": "NPP X"},
             "type": "", "objThucHien": {"t1": 41}, "objTraThuong": {"r1": 2}, "soSuatCT": 1},
            {"_idCT": P1, "ma": "KH2", "ten": "Quán B", "kv": "", "type": "",
             "objThucHien": {"t1": 10}, "objTraThuong": {}},
            {"_idCT": P1, "ma": "KH3", "ten": "Quán C", "kv": "Miền Bắc > Hà Nội", "type": "",
             "objThucHien": {}, "objTraThuong": {}},
        ]
        return [ui.ProgramResult({"_id": P1, "name": "CT Alkaline"}, ui.FINAL, 1, rows, targets, rewards),
                ui.ProgramResult({"_id": P2, "name": "CT trống"}, ui.EMPTY, 1)]

    def test_frames_have_summary_rewards_checks_and_region_sheets(self):
        frames = ui.build_ui_frames(self.results(), date(2026, 10, 1), date(2026, 10, 31), "0")
        self.assertEqual(list(frames)[:3], ["Tong_hop", "Ket_qua", "Kiem_tra"])
        self.assertIn("Miền Trung 1B", frames)
        self.assertIn("Miền Bắc > Hà Nội", frames)  # subdivisions preserved
        self.assertIn(ui.UNKNOWN_REGION, frames)
        summary = frames["Tong_hop"]
        self.assertEqual(len(summary), 3)
        first = summary.iloc[0]
        self.assertEqual(first["Nhà phân phối"], "NPP X")
        self.assertEqual(first["Kết quả"], "Nước 500ml (Thùng): 2")
        self.assertTrue(first["Đạt"])
        self.assertEqual(len(frames["Ket_qua"]), 1)
        checks = frames["Kiem_tra"]
        self.assertIn("Tổng tiền (Đơn giá * Số lượng)", checks["Trạng thái"].tolist())
        self.assertEqual(checks["Số dòng khách hàng"].dropna().tolist(), [3, 0])

    def test_sheet_names_are_excel_safe_and_unique(self):
        used = set(ui.RESERVED_SHEETS)
        self.assertEqual(ui.sheet_name("A/B:C*?", used), "A-B-C--")
        long = "x" * 40
        self.assertEqual(len(ui.sheet_name(long, used)), 31)
        self.assertEqual(ui.sheet_name(long, used), "x" * 27 + " (2)")
        self.assertEqual(ui.sheet_name("Tong_hop", used), "Tong_hop (2)")

    def test_workbook_round_trips_with_existing_writer(self):
        frames = ui.build_ui_frames(self.results(), date(2026, 10, 1), date(2026, 10, 31))
        with tempfile.TemporaryDirectory() as tmp:
            path = bonus.write_workbook(frames, "BaoCaoTraThuong_Current.xlsx", Path(tmp))
            sheets = pd.read_excel(path, sheet_name=None)
        self.assertEqual(len(sheets["Tong_hop"]), 3)


class RunIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_source_resolution(self):
        with patch.dict(os.environ, {**{k: "" for k in WEB_ENV}, "PROMOTION_BONUS_SOURCE": "auto"}):
            self.assertEqual(bonus.resolve_source(), "openapi")
        with patch.dict(os.environ, {**WEB_ENV, "PROMOTION_BONUS_SOURCE": "auto"}):
            self.assertEqual(bonus.resolve_source(), "ui")
        with patch.dict(os.environ, {"PROMOTION_BONUS_SOURCE": "magic"}), self.assertRaises(ValueError):
            bonus.resolve_source()

    def _run(self, results, extra_env=None):
        env = {**WEB_ENV, "DRY_RUN": "true", "PROMOTION_BONUS_SOURCE": "auto",
               "PROMOTION_BONUS_REQUIRE_DMS_MATCH": "true",
               "PROMOTION_BONUS_FROM_DATE": "2026-10-01", "PROMOTION_BONUS_TO_DATE": "2026-10-31",
               "PROMOTION_BONUS_PROGRAM": "all", **(extra_env or {})}
        programs = [{"_id": P1, "orgid": ORG, "name": "CT Alkaline"}]
        with patch.dict(os.environ, env), \
                patch.object(bonus.MobiWorkClient, "from_env", return_value=Mock()), \
                patch.object(bonus, "fetch_programs", return_value=programs) as catalogue, \
                patch.object(ui, "fetch_ui_snapshot", return_value=results) as fetch:
            try:
                return bonus.run(), catalogue, fetch
            finally:
                self.manifest = json.loads(Path("output/promotion_bonus_manifest.json").read_text(encoding="utf-8"))

    def test_ui_source_builds_workbook_even_when_openapi_gate_is_on(self):
        results = WorkbookTests().results()[:1]
        manifest, catalogue, fetch = self._run(results)
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["source"], "ui")
        self.assertEqual(manifest["customer_row_count"], 3)
        self.assertEqual(catalogue.call_args.kwargs["filters"], {"fromdate": "01/10/2026", "todate": "31/10/2026"})
        self.assertEqual(fetch.call_args.args[1:], (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertTrue(Path("output/BaoCaoTraThuong_Current.xlsx").exists())
        self.assertNotIn("SECRET-TOKEN", json.dumps(manifest))

    def test_unresolved_envelopes_block_publication(self):
        stuck = [ui.ProgramResult({"_id": P1, "name": "CT"}, ui.ENVELOPE, 4)]
        with self.assertRaises(RuntimeError):
            self._run(stuck)
        self.assertEqual(self.manifest["status"], "failed")
        self.assertFalse(self.manifest["workbook_published"])
        self.assertEqual(len(self.manifest["unresolved_programs"]), 1)
        manifest, _, _ = self._run(stuck, {"PROMOTION_BONUS_ALLOW_PARTIAL": "true"})
        self.assertEqual(manifest["status"], "success")

    def test_openapi_source_keeps_existing_gate(self):
        env = {**{k: "" for k in WEB_ENV}, "PROMOTION_BONUS_REQUIRE_DMS_MATCH": "true", "DRY_RUN": "true"}
        with patch.dict(os.environ, env), self.assertRaises(RuntimeError):
            bonus.run()

    def test_program_selection(self):
        programs = [{"_id": P1}, {"_id": P2}]
        self.assertEqual(bonus._select_programs(programs, f"{P2}"), [{"_id": P2}])
        self.assertEqual(len(bonus._select_programs(programs, "all")), 2)
        with self.assertRaises(ValueError):
            bonus._select_programs(programs, "c" * 24)


if __name__ == "__main__":
    unittest.main()
