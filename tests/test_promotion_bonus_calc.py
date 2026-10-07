from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

import promotion_bonus as bonus
import promotion_bonus_calc as calc
import promotion_bonus_ui as ui

C1, C2, C3 = "a" * 24, "b" * 24, "c" * 24
OCT1, OCT31 = date(2026, 10, 1), date(2026, 10, 31)


def unit(name):
    return {"viewData": name, "choice_values": name}


def qty_program(pid="1" * 24, minimum=72, maximum=0, units=("Chai",), customers=(C1, C2),
                multiple=False, cttb=None, region="Miền Trung 1"):
    buy = [{"ma_san_pham": "230100110", "ten_san_pham": "Vikoda 500ml", "don_vi_tinh": unit(u),
            "so_luong": 0} for u in units]
    return {"_id": pid, "name": "581/TB/GT/10/2026_CT TRƯNG BÀY PET", "ptype": {"value": "MUTI_SP_SL_SP"},
            "products": [{"san_pham_mua": buy,
                          "san_pham_khuyen_mai": [[{"ma_san_pham": "230100110", "ten_san_pham": "Vikoda 500ml",
                                                    "don_vi_tinh": unit("Chai"), "so_luong": 12}]],
                          "yeu_cau": {"qualityMin": minimum, "qualityMax": maximum},
                          "chon_tat_ca_sp": {"chon_tat_ca_sp": False}}],
            "customer": list(customers), "settings": {"BoiSo": multiple}, "soSuat": "",
            "cttb": cttb, "ctype": {"label": region, "value": region}}


def amount_program(minimum=7_000_000, maximum=10_600_000, customers=(C1,)):
    return {"_id": "2" * 24, "name": "002/TB/GT/01/2026_Q4_CT TÍCH LŨY VIPSHOP", "ptype": {"value": "MUTI_SP_ST_SP"},
            "products": [{"san_pham_mua": [{"ma_san_pham": "230100115", "don_vi_tinh": unit("Chai")}],
                          "san_pham_khuyen_mai": [[{"ma_san_pham": "230100110", "ten_san_pham": "Vikoda 500ml",
                                                    "don_vi_tinh": unit("Chai"), "so_luong": 120}]],
                          "yeu_cau": {"amountMin": minimum, "amountMax": maximum}}],
            "customer": list(customers), "settings": {"BoiSo": False}, "soSuat": "1"}


def bill(rows):
    base = {"ID_khachhang": C1, "ma_kh": "KHHO112323", "ma_sp": "230100110", "ten_dvt": "Chai",
            "ma_dvt": "Chai", "so_luong": 0, "thanh_tien": 0, "ngay_giao_hang": "2026-10-02T17:00:00.000Z",
            "is_km": False}
    return pd.DataFrame([{**base, **r} for r in rows], dtype=object)


class RuleTests(unittest.TestCase):
    def test_parse_quantity_amount_and_single_product_rules(self):
        rule = calc.parse_rule(qty_program(maximum=30))
        self.assertEqual((rule.kind, rule.minimum, rule.maximum), (calc.QUANTITY, 72, 30))
        self.assertEqual(rule.units, {"230100110": frozenset({"Chai"})})
        self.assertEqual(rule.plan_text, " >= 72 < 30")
        self.assertEqual(rule.region, "Miền Trung 1")
        money = calc.parse_rule(amount_program())
        self.assertEqual(money.kind, calc.AMOUNT)
        self.assertEqual(money.plan_text, " >= 7,000,000 - 10,600,000")
        single = calc.parse_rule({"_id": "3" * 24, "name": "569", "ptype": {"value": "SP_SL_SP"},
                                  "products": [{"ma_san_pham": "230100096", "don_vi_tinh": unit("Thùng"),
                                                "yeu_cau": 40, "khuyen_mai": [{"ma_san_pham": "230100096",
                                                                               "don_vi_tinh": unit("Thùng"),
                                                                               "so_luong": 2}]}]})
        self.assertEqual((single.minimum, single.rewards[0][2:]), (40, ("Thùng", 2)))

    def test_unsupported_rules_are_reported_not_guessed(self):
        bad = qty_program()
        bad["ptype"] = {"value": "GR_ST_MIN_SP"}
        results, issues = calc.compute([bad], [], {})
        self.assertEqual(results[0].status, "unsupported")
        self.assertIn("unsupported type", issues[0]["Vấn đề"])
        allsp = qty_program()
        allsp["products"][0]["chon_tat_ca_sp"] = {"chon_tat_ca_sp": True}
        with self.assertRaises(ValueError):
            calc.parse_rule(allsp)

    def test_reached_and_multiples(self):
        rule = calc.parse_rule(qty_program(minimum=16, maximum=30, multiple=True))
        self.assertEqual([calc.reward_multiplier(a, rule) for a in (15, 16, 29, 30)], [0, 1, 1, 0])
        open_rule = calc.parse_rule(qty_program(minimum=16, multiple=True))
        self.assertEqual(calc.reward_multiplier(33, open_rule), 2)
        plain = calc.parse_rule(qty_program(minimum=72))
        self.assertEqual(calc.reward_multiplier(1080, plain), 1)


class InputTests(unittest.TestCase):
    def test_delivery_date_is_converted_to_vietnam_day(self):
        self.assertEqual(calc.local_day("2026-09-30T17:00:00.000Z"), date(2026, 10, 1))
        self.assertEqual(calc.local_day("2026-10-31T16:59:59.000Z"), date(2026, 10, 31))
        self.assertEqual(calc.local_day("2026-10-31T17:00:00.000Z"), date(2026, 11, 1))
        self.assertIsNone(calc.local_day(None))

    def test_sold_lines_drop_promotion_lines_and_out_of_period_deliveries(self):
        detail = bill([{"so_luong": 60}, {"so_luong": 5, "is_km": True},
                       {"so_luong": 9, "ngay_giao_hang": "2026-10-31T17:00:00.000Z"},
                       {"so_luong": 7, "ngay_giao_hang": "2026-09-30T17:00:00.000Z"}])
        lines = calc.sold_lines(detail, OCT1, OCT31)
        self.assertEqual([line["quantity"] for line in lines], [60, 7])
        with self.assertRaises(ValueError):
            calc.sold_lines(detail.drop(columns=["is_km"]), OCT1, OCT31)

    def test_display_pass_prefers_passed_grading(self):
        records = [{"ma_kh": "K1", "ten_ct": "CTTB", "cham_diem": {"chon_1": "Không đạt"}},
                   {"ma_kh": "K1", "ten_ct": "CTTB", "cham_diem": {"dat": "", "chon_1": "Đạt"}},
                   {"ma_kh": "K2", "ten_ct": "CTTB", "cham_diem": {"chon_1": "Không đạt"}},
                   {"ma_kh": "", "ten_ct": "CTTB"}]
        self.assertEqual(calc.display_passes(records),
                         {("K1", "cttb"): "Đạt", ("K2", "cttb"): "Không đạt"})
        summary = calc.display_summary(records, [{"cttb": {"ten": " cttb "}}])
        self.assertEqual((summary["matched_programs"], summary["matched_records"]), (1, 4))
        self.assertNotIn("K1", json.dumps(summary))

    def test_display_state_without_grading_is_reported_raw(self):
        records = [{"ma_kh": "K3", "ten_ct": "CTTB", "tt_cham_diem": 1},
                   {"ma_kh": "K3", "ten_ct": "CTTB"},
                   {"ma_kh": "K4", "ten_ct": "CTTB"}]
        self.assertEqual(calc.display_passes(records),
                         {("K3", "cttb"): "Đã ghi nhận (trạng thái 1)", ("K4", "cttb"): "Đã ghi nhận, chưa chấm"})

    def test_display_pagination_and_repeat_guard(self):
        client = Mock()
        page = [{"ma_kh": f"K{i}", "ten_ct": "T"} for i in range(2)]
        client.get_json.side_effect = [{"data": page}, {"data": page[:1] + [{"ma_kh": "Z", "ten_ct": "T"}]},
                                       {"data": []}]
        self.assertEqual(len(calc.fetch_display_records(client, OCT1, OCT31, page_size=2)), 4)
        self.assertEqual(client.get_json.call_args_list[0].args[1]["tu_ngay"], "01/10/2026")
        client = Mock()
        client.get_json.side_effect = [{"data": page}, {"data": page}]
        with self.assertRaises(ValueError):
            calc.fetch_display_records(client, OCT1, OCT31, page_size=2)


class ComputeTests(unittest.TestCase):
    def test_only_declared_unit_counts_like_dms(self):
        # Verified against DMS: 40 cases + 60 bottles on a bottle programme shows 60.
        detail = bill([{"so_luong": 40, "ten_dvt": "Thùng"}, {"so_luong": 60, "ten_dvt": "Chai"},
                       {"ma_sp": "999", "so_luong": 500}])
        results, _ = calc.compute([qty_program()], calc.sold_lines(detail, OCT1, OCT31), {})
        rows = {row["ma"]: row for row in results[0].rows}
        self.assertEqual(rows["KHHO112323"]["objThucHien"][calc.TARGET_ID], 60)
        self.assertEqual(rows["KHHO112323"]["objTraThuong"], {})
        self.assertEqual(rows[C2]["objThucHien"][calc.TARGET_ID], 0)  # registered, no sales

    def test_amount_programme_uses_line_amount_and_tier(self):
        detail = bill([{"ma_sp": "230100115", "thanh_tien": 7_064_080.8, "so_luong": 1000}])
        results, _ = calc.compute([amount_program()], calc.sold_lines(detail, OCT1, OCT31), {})
        row = results[0].rows[0]
        self.assertAlmostEqual(row["objThucHien"][calc.TARGET_ID], 7_064_080.8)
        self.assertEqual(row["objTraThuong"], {"230100110|Chai": 120})
        frames = ui.build_ui_frames(results, OCT1, OCT31)
        self.assertEqual(frames["Tong_hop"].iloc[0]["Kế hoạch"], " >= 7,000,000 - 10,600,000")
        self.assertEqual(frames["Tong_hop"].iloc[0]["Kết quả"], "Vikoda 500ml(Chai): 120")

    def test_display_requirement_drives_eligibility(self):
        program = qty_program(cttb={"id": "x", "ten": "CTTB PET", "ket_qua": {"label": "Đạt", "value": "Đạt"}})
        detail = bill([{"so_luong": 72}, {"ID_khachhang": C2, "ma_kh": "K2", "so_luong": 144}])
        customers = {C1: {"customer_code": "KHHO112323", "Tên Khách hàng": "Quán A", "Tỉnh": "Khánh Hòa"},
                     C2: {"customer_code": "K2"}}
        displays = calc.display_passes([
            {"ma_kh": "KHHO112323", "ten_ct": "CTTB  PET", "cham_diem": {"chon_1": "Đạt"}},
            {"ma_kh": "K2", "ten_ct": "cttb pet", "cham_diem": {"chon_1": "Không đạt"}}])
        results, _ = calc.compute([program], calc.sold_lines(detail, OCT1, OCT31), customers,
                                  displays=displays)
        extra = {row["ma"]: row["extra"] for row in results[0].rows}
        self.assertEqual(extra["KHHO112323"]["Đủ điều kiện trả thưởng"], "Có")
        self.assertEqual(extra["K2"]["Đủ điều kiện trả thưởng"], "Cần kiểm tra trưng bày")
        self.assertEqual(extra["K2"]["Kết quả trưng bày"], "Không đạt")
        diag = calc.diagnostics(results)[0]
        self.assertEqual((diag["registered"], diag["reached"], diag["display_passed"], diag["eligible"]),
                         (2, 2, 1, 1))
        frames = ui.build_ui_frames(results, OCT1, OCT31)
        self.assertIn("Đủ điều kiện trả thưởng", frames["Tong_hop"].columns)
        self.assertIn("Đủ điều kiện trả thưởng", frames["Ket_qua"].columns)
        self.assertEqual(frames["Tong_hop"].iloc[0]["Tên khách hàng"], "Quán A")
        self.assertIn("Miền Trung 1", frames)

    def test_missing_display_data_is_explicit(self):
        program = qty_program(cttb={"ten": "CTTB PET", "ket_qua": {"label": "Đạt"}})
        results, _ = calc.compute([program], calc.sold_lines(bill([{"so_luong": 80}]), OCT1, OCT31), {})
        row = next(r for r in results[0].rows if r["ma"] == "KHHO112323")
        self.assertEqual(row["extra"]["Kết quả trưng bày"], "Chưa có dữ liệu")
        self.assertEqual(row["extra"]["Đủ điều kiện trả thưởng"], "Cần kiểm tra trưng bày")
        self.assertTrue(row["objTraThuong"])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_calc_source_builds_current_and_monthly_workbooks(self):
        env = {"DRY_RUN": "true", "PROMOTION_BONUS_SOURCE": "auto", "PROMOTION_BONUS_FROM_DATE": "2026-10-01",
               "PROMOTION_BONUS_TO_DATE": "2026-10-31", "PROMOTION_BONUS_PROGRAM": "all",
               "PROMOTION_BONUS_REQUIRE_DMS_MATCH": "true"}
        program = qty_program(cttb={"ten": "CTTB PET", "ket_qua": {"label": "Đạt"}})
        catalogue = {"customer_catalogue": {C1: {"customer_code": "KHHO112323"}},
                     "customer_catalogue_audit": {"count": 1}}
        with patch.dict(os.environ, env), \
                patch.object(bonus.MobiWorkClient, "from_env", return_value=Mock()), \
                patch.object(bonus, "fetch_programs", return_value=[program]) as fetch, \
                patch.object(bonus, "_bill_detail", return_value=bill([{"so_luong": 72}])), \
                patch("customer_catalogue.enrich_customer_config", return_value=catalogue), \
                patch.object(calc, "fetch_display_records",
                             return_value=[{"ma_kh": "KHHO112323", "ten_ct": "CTTB PET",
                                            "cham_diem": {"chon_1": "Đạt"}}]):
            manifest = bonus.run()
        self.assertEqual(manifest["source"], "calc")
        self.assertEqual(fetch.call_args.kwargs["filters"], {"fromdate": "01/10/2026", "todate": "31/10/2026"})
        self.assertEqual((manifest["reached_rows"], manifest["eligible_rows"]), (1, 1))
        self.assertTrue(Path("output/BaoCaoTraThuong_Current.xlsx").exists())
        self.assertTrue(Path("output/BaoCaoTraThuong_2026-10.xlsx").exists())
        sheets = pd.read_excel("output/BaoCaoTraThuong_Current.xlsx", sheet_name=None)
        self.assertEqual(len(sheets["Tong_hop"]), 2)
        saved = json.loads(Path("output/promotion_bonus_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "success")

    def test_display_failure_does_not_block_report(self):
        env = {"DRY_RUN": "true", "PROMOTION_BONUS_SOURCE": "calc", "PROMOTION_BONUS_FROM_DATE": "2026-10-01",
               "PROMOTION_BONUS_TO_DATE": "2026-10-31"}
        program = qty_program(cttb={"ten": "CTTB PET", "ket_qua": {"label": "Đạt"}})
        with patch.dict(os.environ, env), \
                patch.object(bonus.MobiWorkClient, "from_env", return_value=Mock()), \
                patch.object(bonus, "fetch_programs", return_value=[program]), \
                patch.object(bonus, "_bill_detail", return_value=bill([])), \
                patch("customer_catalogue.enrich_customer_config",
                      return_value={"customer_catalogue": {}, "customer_catalogue_audit": {"count": 0}}), \
                patch.object(calc, "fetch_display_records", side_effect=PermissionError("denied")):
            manifest = bonus.run()
        self.assertEqual(manifest["status"], "success")
        self.assertIn("PermissionError", manifest["display_error"])

    def test_unstable_customer_catalogue_falls_back_to_bill_identity(self):
        env = {"DRY_RUN": "true", "PROMOTION_BONUS_SOURCE": "calc", "PROMOTION_BONUS_FROM_DATE": "2026-10-01",
               "PROMOTION_BONUS_TO_DATE": "2026-10-31"}
        with patch.dict(os.environ, env), \
                patch.object(bonus.MobiWorkClient, "from_env", return_value=Mock()), \
                patch.object(bonus, "fetch_programs", return_value=[qty_program()]), \
                patch.object(bonus, "_bill_detail", return_value=bill([{"so_luong": 80}])), \
                patch("customer_catalogue.enrich_customer_config",
                      side_effect=ValueError("Customer catalogue total changed")) as cat:
            manifest = bonus.run()
        self.assertEqual(cat.call_count, 1)
        self.assertEqual(manifest["status"], "success")
        sheet = pd.read_excel("output/BaoCaoTraThuong_Current.xlsx", sheet_name="Tong_hop", dtype=object)
        self.assertIn("KHHO112323", sheet["Mã khách hàng"].tolist())
        notes = pd.read_excel("output/BaoCaoTraThuong_Current.xlsx", sheet_name="Kiem_tra", dtype=object)
        self.assertTrue(notes["Trạng thái"].astype(str).str.contains("Không tải ổn định").any())

    def test_period_must_be_one_month(self):
        env = {"DRY_RUN": "true", "PROMOTION_BONUS_SOURCE": "calc", "PROMOTION_BONUS_FROM_DATE": "2026-09-15",
               "PROMOTION_BONUS_TO_DATE": "2026-10-15"}
        with patch.dict(os.environ, env), \
                patch.object(bonus.MobiWorkClient, "from_env", return_value=Mock()), \
                self.assertRaises(ValueError):
            bonus.run()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def patches(self, env, bill_side_effect):
        catalogue = {"customer_catalogue": {C1: {"customer_code": "KHHO112323"}},
                     "customer_catalogue_audit": {"count": 1}}
        return [patch.dict(os.environ, env),
                patch.object(bonus.MobiWorkClient, "from_env", return_value=Mock()),
                patch.object(bonus, "fetch_programs", return_value=[qty_program()]),
                patch.object(bonus, "_bill_detail", side_effect=bill_side_effect),
                patch("customer_catalogue.enrich_customer_config", return_value=catalogue)]

    def run_with(self, env, bill_side_effect):
        from contextlib import ExitStack
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in self.patches(env, bill_side_effect)]
            return bonus.run(), mocks

    def test_explicit_months_write_monthly_files_only_and_skip_missing_month(self):
        def bill_for(first, dry, manifest):
            if first.month == 7:
                raise ValueError("Bill monthly master missing")
            return bill([{"so_luong": 80, "ngay_giao_hang": f"2026-{first.month:02d}-10T03:00:00.000Z"}])
        env = {"DRY_RUN": "true", "PROMOTION_BONUS_SOURCE": "calc",
               "PROMOTION_BONUS_MONTHS": "2026-09, 2026-08,2026-07"}
        manifest, mocks = self.run_with(env, bill_for)
        self.assertEqual(manifest["storage_mode"], "monthly_history")
        self.assertTrue(Path("output/BaoCaoTraThuong_2026-09.xlsx").exists())
        self.assertTrue(Path("output/BaoCaoTraThuong_2026-08.xlsx").exists())
        self.assertFalse(Path("output/BaoCaoTraThuong_Current.xlsx").exists())
        self.assertIn("error", manifest["months"]["2026-07"])
        self.assertEqual(manifest["months"]["2026-09"]["reached_rows"], 1)
        filters = [c.kwargs["filters"] for c in mocks[2].call_args_list]
        self.assertIn({"fromdate": "01/08/2026", "todate": "31/08/2026"}, filters)
        self.assertEqual(mocks[4].call_count, 1)  # customer catalogue fetched once

    def test_past_month_dates_do_not_overwrite_current(self):
        env = {"DRY_RUN": "true", "PROMOTION_BONUS_SOURCE": "calc",
               "PROMOTION_BONUS_FROM_DATE": "2026-09-01", "PROMOTION_BONUS_TO_DATE": "2026-09-30"}
        with patch.object(bonus.ui, "report_period", wraps=bonus.ui.report_period):
            manifest, _ = self.run_with(env, lambda *a: bill([]))
        self.assertEqual(manifest["storage_mode"], "monthly_history")
        self.assertTrue(Path("output/BaoCaoTraThuong_2026-09.xlsx").exists())
        self.assertFalse(Path("output/BaoCaoTraThuong_Current.xlsx").exists())

    def test_history_publish_uses_month_folders_and_history_state(self):
        sharepoint = Mock()
        sharepoint.upload_file.return_value = {}
        env = {"DRY_RUN": "false", "PROMOTION_BONUS_SOURCE": "calc", "SHAREPOINT_DRIVE_ID": "drive",
               "PROMOTION_BONUS_MONTHS": "2026-08,2026-09"}
        with patch.object(bonus.SemanticSharePointClient, "from_env", return_value=sharepoint):
            manifest, _ = self.run_with(env, lambda *a: bill([]))
        folders = [c.args[2] for c in sharepoint.upload_file.call_args_list]
        self.assertEqual(folders, ["06_BaoCaoTraThuong/2026/08", "06_BaoCaoTraThuong/2026/09"])
        self.assertTrue(sharepoint.upload_json.call_args.args[1].endswith("promotion_bonus_history.json"))
        self.assertTrue(manifest["workbook_published"])

    def test_locked_month_file_does_not_block_other_months(self):
        sharepoint = Mock()
        sharepoint.upload_file.side_effect = [RuntimeError("423 Locked"), {}]
        env = {"DRY_RUN": "false", "PROMOTION_BONUS_SOURCE": "calc", "SHAREPOINT_DRIVE_ID": "drive",
               "PROMOTION_BONUS_MONTHS": "2026-08,2026-09"}
        with patch.object(bonus.SemanticSharePointClient, "from_env", return_value=sharepoint):
            manifest, _ = self.run_with(env, lambda *a: bill([]))
        self.assertEqual(manifest["published_files"], ["06_BaoCaoTraThuong/2026/09/BaoCaoTraThuong_2026-09.xlsx"])
        self.assertIn("423", manifest["publish_failures"][0])
        self.assertEqual(manifest["status"], "success")

    def test_all_history_uploads_failing_is_an_error(self):
        sharepoint = Mock()
        sharepoint.upload_file.side_effect = RuntimeError("423 Locked")
        env = {"DRY_RUN": "false", "PROMOTION_BONUS_SOURCE": "calc", "SHAREPOINT_DRIVE_ID": "drive",
               "PROMOTION_BONUS_MONTHS": "2026-08"}
        with patch.object(bonus.SemanticSharePointClient, "from_env", return_value=sharepoint), \
                self.assertRaises(RuntimeError):
            self.run_with(env, lambda *a: bill([]))

    def test_current_snapshot_upload_failure_still_fails(self):
        sharepoint = Mock()
        sharepoint.upload_file.side_effect = [RuntimeError("423 Locked"), {}]
        today = bonus.datetime.now(bonus.ui.VN_TZ).date()
        env = {"DRY_RUN": "false", "PROMOTION_BONUS_SOURCE": "calc", "SHAREPOINT_DRIVE_ID": "drive",
               "PROMOTION_BONUS_FROM_DATE": today.replace(day=1).isoformat(),
               "PROMOTION_BONUS_TO_DATE": today.isoformat()}
        with patch.object(bonus.SemanticSharePointClient, "from_env", return_value=sharepoint), \
                self.assertRaises(RuntimeError):
            self.run_with(env, lambda *a: bill([]))

    def test_archive_copy_lock_does_not_fail_current_snapshot(self):
        sharepoint = Mock()
        sharepoint.upload_file.side_effect = [{}, RuntimeError("423 Locked")]
        today = bonus.datetime.now(bonus.ui.VN_TZ).date()
        env = {"DRY_RUN": "false", "PROMOTION_BONUS_SOURCE": "calc", "SHAREPOINT_DRIVE_ID": "drive",
               "PROMOTION_BONUS_FROM_DATE": today.replace(day=1).isoformat(),
               "PROMOTION_BONUS_TO_DATE": today.isoformat()}
        with patch.object(bonus.SemanticSharePointClient, "from_env", return_value=sharepoint):
            manifest, _ = self.run_with(env, lambda *a: bill([]))
        self.assertEqual(manifest["published_files"], ["06_BaoCaoTraThuong/BaoCaoTraThuong_Current.xlsx"])
        self.assertEqual(len(manifest["publish_failures"]), 1)

    def test_all_existing_skips_current_month(self):
        today = bonus.datetime.now(bonus.ui.VN_TZ).date()
        current = today.replace(day=1)
        from datetime import timedelta
        previous = (current - timedelta(days=1)).replace(day=1)
        with patch("promotion_months.discover_bill_months", return_value=[previous, current]), \
                patch.object(bonus.SemanticSharePointClient, "from_env", return_value=Mock()), \
                patch.dict(os.environ, {"SHAREPOINT_DRIVE_ID": "drive"}):
            self.assertEqual(bonus._history_months("all_existing", False), [previous])

    def test_invalid_month_list(self):
        with self.assertRaises(ValueError):
            bonus._history_months(" , ", True)
        with self.assertRaises(ValueError):
            bonus._history_months("all_existing", True)
        self.assertEqual([m.isoformat() for m in bonus._history_months("2026-09,2026-08,2026-09", True)],
                         ["2026-08-01", "2026-09-01"])


if __name__ == "__main__":
    unittest.main()
