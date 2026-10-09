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
            "is_km": False, "loai_hang": "Bán hàng", "ma_phieu": "DH1", "ten_sp": "Vikoda 500ml",
            "gia_truoc_vat": 4541.67, "ma_nv_dat": "KHHO0303", "ten_nguoi_dat": "NV A",
            "ngay_dat": "2026-10-02 08:00:00", "ten_kh": "Quán A", "dia_chi": "Nha Trang", "sdt": "09",
            "tuyen_code": "03"}
    return pd.DataFrame([{**base, "stt": str(i), **r} for i, r in enumerate(rows, 1)], dtype=object)


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
    def test_confirmed_line_unit_is_used_for_the_calculation(self):
        detail = bill([{"ten_dvt": "", "ma_dvt": "", "so_luong": 80}])
        cfg = {"line_unit_overrides": {"DH1|1|230100110": {"unit": "Chai", "source": "User confirmation"}}}
        lines = calc.sold_lines(detail, OCT1, OCT31, cfg)
        results, _ = calc.compute([qty_program()], lines, {})
        self.assertEqual(results[0].rows[0]["objThucHien"][calc.TARGET_ID], 80)

    def test_invalid_sale_values_are_rejected_instead_of_becoming_zero_or_nan(self):
        for row in ({"so_luong": float("nan")}, {"thanh_tien": float("inf")},
                    {"so_luong": True}, {"ID_khachhang": float("nan")},
                    {"ma_sp": ""}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                calc.sold_lines(bill([row]), OCT1, OCT31)

    def test_missing_unit_blocks_only_a_qualifying_programme_line(self):
        detail = bill([{"so_luong": 80, "ten_dvt": float("nan"), "ma_dvt": ""}])
        lines = calc.sold_lines(detail, OCT1, OCT31)
        results, _ = calc.compute([qty_program()], lines, {})
        held = results[0].rows[0]
        self.assertEqual(held["_eligible"], calc.UNIT_HOLD)  # held, never paid nor "Không"
        self.assertEqual((held["_rewards"], held["objTraThuong"]), ([], {}))
        self.assertEqual(results[0].rows[1]["_eligible"], "Không")  # other customers still computed
        unrelated = bill([{"ma_sp": "OTHER", "ten_dvt": float("nan"), "ma_dvt": ""}])
        results, _ = calc.compute([qty_program()], calc.sold_lines(unrelated, OCT1, OCT31), {})
        self.assertEqual(results[0].rows[0]["objThucHien"][calc.TARGET_ID], 0)

    def test_confirmed_sku_default_qualifies_sales_from_new_orders(self):
        detail = bill([{"ma_phieu": "NEW1", "ten_dvt": "", "ma_dvt": "", "so_luong": 80},
                       {"ma_phieu": "NEW2", "ten_dvt": "", "ma_dvt": "", "so_luong": 40}])
        cfg = {"sale_unit_defaults": {"230100110": {"unit": "Chai", "source": "Confirmed default"}}}
        lines = calc.sold_lines(detail, OCT1, OCT31, cfg)
        results, issues = calc.compute([qty_program()], lines, {})
        self.assertFalse(issues)
        self.assertEqual(results[0].rows[0]["objThucHien"][calc.TARGET_ID], 120)
        self.assertEqual(results[0].rows[0]["_eligible"], "Có")
        self.assertEqual(len(results[0].rows[0]["_lines"]), 2)

    def test_missing_unit_diagnostics_include_all_qualifying_keys_without_customer_fields(self):
        detail = bill([
            {"ma_phieu": "BH1", "ten_dvt": "", "ma_dvt": "", "so_luong": 80},
            {"ma_phieu": "BH2", "ma_sp": "OTHER", "ten_dvt": "", "ma_dvt": "", "so_luong": 90},
            {"ma_phieu": "UNRELATED", "ma_sp": "UNLISTED", "ten_dvt": "", "ma_dvt": ""},
        ])
        other = qty_program(pid="2" * 24)
        other["name"] = "588/TB/GT/10/2026_OTHER"
        other["products"][0]["san_pham_mua"][0]["ma_san_pham"] = "OTHER"
        results, _ = calc.compute([qty_program(), qty_program(), other], calc.sold_lines(detail, OCT1, OCT31), {})
        payload = calc.unit_gap_summary(results)
        text = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["missing_unit_line_count"], 2)
        self.assertEqual([(r["order"], r["line"], r["sku"], r["quantity"]) for r in payload["lines"]],
                         [("BH1", "1", "230100110", 80), ("BH2", "2", "OTHER", 90)])
        self.assertEqual(len(payload["lines"][0]["programmes"]), 1)
        self.assertNotIn("UNRELATED", text)
        for field in (C1, "Quán A", "Nha Trang", "ten_kh", "sdt", "dia_chi"):
            self.assertNotIn(field, text)

    def test_missing_unit_diagnostics_are_bounded_but_report_the_full_count(self):
        detail = bill([{"ten_dvt": "", "ma_dvt": ""} for _ in range(30)])
        results, _ = calc.compute([qty_program()], calc.sold_lines(detail, OCT1, OCT31), {})
        payload = calc.unit_gap_summary(results)
        self.assertEqual((payload["missing_unit_line_count"], len(payload["lines"])), (30, 25))

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
        self.assertIn(ui.UNKNOWN_REGION, frames)

    def test_missing_display_data_is_explicit(self):
        program = qty_program(cttb={"ten": "CTTB PET", "ket_qua": {"label": "Đạt"}})
        results, _ = calc.compute([program], calc.sold_lines(bill([{"so_luong": 80}]), OCT1, OCT31), {})
        row = next(r for r in results[0].rows if r["ma"] == "KHHO112323")
        self.assertEqual(row["extra"]["Kết quả trưng bày"], "Chưa có dữ liệu")
        self.assertEqual(row["extra"]["Đủ điều kiện trả thưởng"], "Cần kiểm tra trưng bày")
        self.assertFalse(row["objTraThuong"])
        self.assertTrue(row["objThuongDuKien"])
        frames = ui.build_ui_frames(results, OCT1, OCT31)
        self.assertTrue(frames["Ket_qua"].empty)
        self.assertTrue(frames["Tong_hop"]["Thưởng dự kiến"].str.contains("12").any())
        detail = calc.detail_source(results, "10/2026")[0][1]
        self.assertNotIn(calc.GIFT_ORDER, detail["ma_phieu"].tolist())

    def test_region_uses_customer_or_unambiguous_sales_assignment(self):
        lines = calc.sold_lines(bill([{"so_luong": 80}]), OCT1, OCT31)
        mappings, conflicts = calc.customer_sales_metadata(
            lines, {"KHHO0303": {"Vùng": "Miền Trung 1B", "Tên NPP": "NPP A"}})
        results, _ = calc.compute([qty_program(region="Tất cả")], lines, {}, sales_metadata=mappings)
        row = results[0].rows[0]
        self.assertEqual((row["kv"], row["npp"], conflicts), ("Miền Trung 1B", "NPP A", 0))
        self.assertEqual(row["extra"]["Vùng áp dụng CT"], "Tất cả")
        other = calc.sold_lines(bill([{"ma_nv_dat": "NV2"}]), OCT1, OCT31)
        mappings, conflicts = calc.customer_sales_metadata(
            lines + other, {"KHHO0303": {"Vùng": "Miền Trung 1B"}, "NV2": {"Vùng": "Miền Bắc"}})
        self.assertEqual((mappings[C1], conflicts), ({}, 1))



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

    def test_source_errors_block_publication_but_dry_run_keeps_diagnostics(self):
        env = {"PROMOTION_BONUS_SOURCE": "calc", "PROMOTION_BONUS_FROM_DATE": "2026-10-01",
               "PROMOTION_BONUS_TO_DATE": "2026-10-31"}
        with patch.dict(os.environ, env), \
                patch.object(bonus, "fetch_programs", return_value=[qty_program()]), \
                patch.object(bonus, "_bill_detail", return_value=bill([{"so_luong": 80, "ngay_dat": "invalid"}])):
            audit = {}
            frames = bonus._calc_month_frames(Mock(), bonus.load_config(), OCT1, OCT31, True,
                                             {}, "test", audit, detail_config={})
            self.assertGreater(audit["blocking_issues"], 0)
            self.assertIn("Chặn xuất bản", set(frames["CanBoSung"]["Mức độ"]))
            with self.assertRaisesRegex(ValueError, "Nothing published"):
                bonus._calc_month_frames(Mock(), bonus.load_config(), OCT1, OCT31, False,
                                         {}, "test", {}, detail_config={})

    def test_unsupported_calculation_option_is_not_silently_ignored(self):
        with patch.dict(os.environ, {"PROMOTION_BONUS_STTT": "2"}), \
                patch.object(bonus, "fetch_programs") as fetch, \
                self.assertRaisesRegex(ValueError, "only sttt=0"):
            bonus._calc_month_frames(Mock(), bonus.load_config(), OCT1, OCT31, True, {}, "", {})
        fetch.assert_not_called()

    def test_missing_pack_mapping_keeps_quantity_blank_without_blocking_report(self):
        with patch.dict(os.environ, {"PROMOTION_BONUS_STTT": "0"}), \
                patch.object(bonus, "fetch_programs", return_value=[qty_program()]), \
                patch.object(bonus, "_bill_detail", return_value=bill([{"so_luong": 80}])):
            audit = {}
            frames = bonus._calc_month_frames(Mock(), bonus.load_config(), OCT1, OCT31, False,
                                             {}, "test", audit, detail_config={})
        self.assertEqual(audit["blocking_issues"], 0)
        self.assertGreater(audit["unit_gaps"], 0)
        self.assertTrue(frames["BaoCao"]["Số lượng SELL-OUT"].isna().all())
        self.assertIn("Thiếu quy đổi đơn vị", set(frames["CanBoSung"]["Mức độ"]))
        self.assertEqual(audit["quality_status"], "needs_review")


class TemplateTests(unittest.TestCase):
    def test_bonus_codes_keep_levels(self):
        self.assertEqual(calc.bonus_code("581/TB/GT/10/2026 _CHƯƠNG TRÌNH TRƯNG BÀY PET"), "581/TB/GT/10/2026")
        self.assertEqual(calc.bonus_code("008/TB/GT/01/2026_Q4_CT TÍCH LŨY VIPSHOP THEO THÁNG - MỨC 2"),
                         "008/TB/GT/01/2026_Q4 - Mức 2")
        self.assertEqual(calc.bonus_code("575/TB/GT/10/2026_MỨC 1--CT TÍCH LŨY"), "575/TB/GT/10/2026 - Mức 1")
        self.assertEqual(calc.bonus_code("Chương trình lạ"), "Chương trình lạ")

    def test_detail_source_repeats_counted_lines_and_adds_reward_line(self):
        detail = bill([{"so_luong": 40, "ten_dvt": "Thùng"}, {"so_luong": 80}, {"ma_sp": "999", "so_luong": 5}])
        program = qty_program(cttb={"ten": "CTTB", "ket_qua": {"label": "Đạt"}})
        results, _ = calc.compute([program], calc.sold_lines(detail, OCT1, OCT31), {},
                                  displays={("KHHO112323", "cttb"): "Đạt"})
        sources = calc.detail_source(results, "10/2026")
        self.assertEqual(len(sources), 1)
        code, frame = sources[0]
        self.assertEqual(code, "581/TB/GT/10/2026")
        self.assertEqual(frame["ma_phieu"].tolist(), ["DH1", calc.GIFT_ORDER])  # only the bottle line counts
        gift = frame.iloc[-1]
        self.assertEqual((gift["ma_sp"], gift["so_luong"], gift["ten_dvt"], gift["is_km"]),
                         ("230100110", 12, "Chai", True))
        self.assertNotIn("cần kiểm tra trưng bày", gift["ten_sp"])

    def test_template_report_uses_ctkm_layout_and_pack_units(self):
        detail = bill([{"so_luong": 72, "thanh_tien": 326_000}])
        results, _ = calc.compute([qty_program()], calc.sold_lines(detail, OCT1, OCT31), {})
        with patch("promotion_detail.enrich_product_config", side_effect=RuntimeError("offline")):
            report, issues = bonus._template_report(Mock(), {}, results, OCT1)
        from promotion_detail import BONUS_COLUMNS, COLUMNS
        self.assertEqual(list(report.columns), COLUMNS + list(BONUS_COLUMNS))
        sale, gift = report.iloc[0], report.iloc[1]
        self.assertEqual(sale["Mã CTKM"], "581/TB/GT/10/2026")
        self.assertEqual(sale["Mã Khách hàng"], "KHHO112323")
        self.assertAlmostEqual(sale["Số lượng SELL-OUT"], 3.0)  # 72 bottles = 3 cases of 24
        self.assertEqual(gift["Mã Đơn hàng"], calc.GIFT_ORDER)
        self.assertEqual(gift["Sản phẩm Tặng"], "230100110")
        self.assertAlmostEqual(gift["Số lượng Khuyến mãi"], 0.5)  # 12 bottles = 0.5 case

    def test_allocation_adds_up_exactly(self):
        parts = calc.allocate([1, 1, 1], 100, 0)
        self.assertEqual(sum(parts), 100)
        self.assertEqual(calc.allocate([30, 10], 12, 4), [9.0, 3.0])
        self.assertEqual(calc.allocate([0, 0], 10, 0), [5, 5])
        self.assertEqual(calc.allocate([], 10, 0), [])

    def test_reward_is_allocated_to_each_order(self):
        detail = bill([{"so_luong": 48, "thanh_tien": 240_000, "ma_phieu": "DH1"},
                       {"so_luong": 24, "thanh_tien": 120_000, "ma_phieu": "DH2"},
                       {"so_luong": 24, "thanh_tien": 120_000, "ma_phieu": "DH2"}])
        lines = calc.sold_lines(detail, OCT1, OCT31)
        results, _ = calc.compute([qty_program()], lines, {})
        prices = calc.average_prices(lines)
        self.assertEqual(prices[("230100110", "Chai")], 5000)
        with patch("promotion_detail.enrich_product_config", side_effect=RuntimeError("offline")):
            report, _ = bonus._template_report(Mock(), {}, results, OCT1, prices=prices)
        sales = report[report["Mã Đơn hàng"] != calc.GIFT_ORDER]
        self.assertEqual(list(sales["Tiền thưởng phân bổ (đ)"]), [30_000, 15_000, 15_000])  # 12 chai × 5.000
        self.assertEqual(sales.iloc[0]["Thưởng phân bổ theo đơn"], "Vikoda 500ml (Chai): 6")
        gift = report[report["Mã Đơn hàng"] == calc.GIFT_ORDER].iloc[0]
        self.assertTrue(pd.isna(gift["Tiền thưởng phân bổ (đ)"]))  # no double counting
        orders = bonus.reward_by_order(report)
        self.assertEqual(list(orders["Mã Đơn hàng"]), ["DH1", "DH2"])
        self.assertEqual(list(orders["Tiền thưởng phân bổ (đ)"]), [30_000, 30_000])
        self.assertEqual(list(orders["Số dòng hàng"]), [1, 2])

    def test_money_programme_and_unpriced_gift(self):
        program = amount_program(minimum=300_000, maximum=0)
        program["ptype"] = {"value": calc.MONEY_TYPE}
        program["products"][0]["khuyen_mai"] = "550000"
        program["products"][0]["san_pham_mua"][0]["ma_san_pham"] = "230100110"
        detail = bill([{"so_luong": 50, "thanh_tien": 200_000, "ma_phieu": "DH1"},
                       {"so_luong": 50, "thanh_tien": 150_000, "ma_phieu": "DH2"}])
        results, _ = calc.compute([program], calc.sold_lines(detail, OCT1, OCT31), {})
        sources = calc.detail_source(results, "10/2026", {})
        frame = sources[0][1]
        sales = frame[frame["ma_phieu"] != calc.GIFT_ORDER]
        self.assertEqual(list(sales[calc.BONUS_VALUE]), [314_286, 235_714])
        results, _ = calc.compute([qty_program()], calc.sold_lines(bill([{"so_luong": 72}]), OCT1, OCT31), {})
        frame = calc.detail_source(results, "10/2026", {})[0][1]
        self.assertIsNone(frame.iloc[0][calc.BONUS_VALUE])  # no selling price → no guessed value
        self.assertEqual(frame.iloc[0][calc.BONUS_TEXT], "Vikoda 500ml (Chai): 12")

    def test_unpaid_customer_gets_no_allocation(self):
        results, _ = calc.compute([qty_program(minimum=500)], calc.sold_lines(bill([{"so_luong": 72}]), OCT1, OCT31), {})
        frame = calc.detail_source(results, "10/2026", {})
        self.assertTrue(all(f.empty or f[calc.BONUS_VALUE].isna().all() for _, f in frame))

    def test_template_keeps_full_level_code(self):
        program = qty_program()
        program["name"] = "008/TB/GT/01/2026_Q4_CT TÍCH LŨY VIPSHOP THEO THÁNG - MỨC 2"
        results, _ = calc.compute([program], calc.sold_lines(bill([{"so_luong": 80}]), OCT1, OCT31), {})
        with patch("promotion_detail.enrich_product_config", side_effect=RuntimeError("offline")):
            report, _ = bonus._template_report(Mock(), {}, results, OCT1)
        self.assertEqual(set(report["Mã CTKM"]), {"008/TB/GT/01/2026_Q4 - Mức 2"})

    def test_workbook_first_sheet_is_template(self):
        import openpyxl
        detail = bill([{"so_luong": 72}])
        results, _ = calc.compute([qty_program()], calc.sold_lines(detail, OCT1, OCT31), {})
        with tempfile.TemporaryDirectory() as tmp, \
                patch("promotion_detail.enrich_product_config", side_effect=RuntimeError("offline")):
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                report, _ = bonus._template_report(Mock(), {}, results, OCT1)
                path = bonus._write_bonus({"BaoCao": report, "Tong_hop": ui.build_ui_frames(results, OCT1, OCT31)["Tong_hop"]},
                                          "x.xlsx", OCT1)
                book = openpyxl.load_workbook(path)
            finally:
                os.chdir(cwd)
        sheet = book.worksheets[0]
        self.assertEqual(sheet.title, "BaoCao")
        self.assertEqual(sheet["A3"].value, "BÁO CÁO TRẢ THƯỞNG CHI TIẾT THEO KHÁCH HÀNG - THÁNG 10/2026")
        self.assertEqual(sheet["A4"].value, "Vùng")
        self.assertEqual(sheet["J5"].value, "581/TB/GT/10/2026")
        self.assertEqual(sheet.freeze_panes, "A5")
        self.assertIn("Tong_hop", book.sheetnames)


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

    def test_missing_unit_line_publishes_report_and_holds_customer(self):
        def bill_for(first, dry, manifest):
            return bill([{"so_luong": 80, "ten_dvt": "", "ma_dvt": "",
                          "ngay_giao_hang": f"2026-{first.month:02d}-10T03:00:00.000Z"}])
        env = {"DRY_RUN": "true", "PROMOTION_BONUS_SOURCE": "calc", "PROMOTION_BONUS_MONTHS": "2026-09",
               "GITHUB_ACTIONS": "true"}
        manifest, _ = self.run_with(env, bill_for)
        month = manifest["months"]["2026-09"]
        self.assertNotIn("error", month)
        self.assertEqual((month["unit_gap_lines"], month["unit_gap_held_rows"]), (1, 1))
        self.assertTrue(Path("output/BaoCaoTraThuong_2026-09.xlsx").exists())

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

    def test_locked_current_snapshot_keeps_run_green_when_month_file_publishes(self):
        sharepoint = Mock()
        sharepoint.upload_file.side_effect = [RuntimeError("423 Locked"), {}]
        today = bonus.datetime.now(bonus.ui.VN_TZ).date()
        env = {"DRY_RUN": "false", "PROMOTION_BONUS_SOURCE": "calc", "SHAREPOINT_DRIVE_ID": "drive",
               "PROMOTION_BONUS_FROM_DATE": today.replace(day=1).isoformat(),
               "PROMOTION_BONUS_TO_DATE": today.isoformat()}
        with patch.object(bonus.SemanticSharePointClient, "from_env", return_value=sharepoint):
            manifest, _ = self.run_with(env, lambda *a: bill([]))
        self.assertTrue(manifest["current_snapshot_locked"])
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(len(manifest["published_files"]), 1)
        self.assertNotIn("Current", manifest["published_files"][0])

    def test_current_snapshot_upload_failure_still_fails(self):
        sharepoint = Mock()
        sharepoint.upload_file.side_effect = [RuntimeError("500 Server Error"), {}]
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


def summer_program(tier="A", minimum=11_520_000, maximum=23_040_000, customers=(C1,)):
    """246/248 shape: buy amount over 01/04–30/09, reward = cash (khuyen_mai)."""
    start = int(pd.Timestamp("2026-04-01", tz="Asia/Ho_Chi_Minh").timestamp() * 1000)
    end = int(pd.Timestamp("2026-09-30 23:59:59", tz="Asia/Ho_Chi_Minh").timestamp() * 1000)
    return {"_id": tier * 24, "name": f"248/TB/GT/04/2026_CHƯƠNG TRÌNH ĐẢNH THẠNH MÙA HÈ - LOẠI {tier}(Miền Bắc)",
            "ptype": {"value": "MUTI_SP_ST_TIEN", "label": "Mua nhiều sản phẩm - đạt số tiền - tặng tiền"},
            "products": [{"san_pham_mua": [{"ma_san_pham": "230100008", "ten_san_pham": "Đảnh Thạnh RGB",
                                            "don_vi_tinh": unit("Két")}],
                          "yeu_cau": {"amountMin": minimum, "amountMax": maximum},
                          "khuyen_mai": "550000", "chon_tat_ca_sp": {"chon_tat_ca_sp": False}}],
            "customer": list(customers), "settings": {"BoiSo": False}, "soSuat": 25,
            "startDate": start, "endDate": end, "ctype": {"label": "Tất cả"}}


class CumulativeMoneyProgramTests(unittest.TestCase):
    def test_cash_reward_rule_and_tier_code(self):
        rule = calc.parse_rule(summer_program())
        self.assertEqual((rule.kind, rule.minimum, rule.maximum), (calc.AMOUNT, 11_520_000, 23_040_000))
        self.assertEqual(rule.rewards, (("TIEN", "Tiền thưởng", "đồng", 550_000.0),))
        self.assertEqual(calc.bonus_code(rule.name), "248/TB/GT/04/2026 - Loại A")

    def _frames(self, first, last, months):
        def detail(month, dry_run, manifest):
            key = f"{month:%Y-%m}"
            if key not in months:
                raise ValueError("Bill monthly master missing")
            return months[key]
        bonus._BILL_CACHE.clear()
        cfg = bonus.PromotionBonusConfig(**{**json.loads(Path("config/promotion_bonus.json").read_text(
            encoding="utf-8")), "cumulative_programs": ("248/TB/GT/04/2026",)})
        with patch.dict(os.environ, {"PROMOTION_BONUS_STTT": "0"}), \
                patch.object(bonus, "fetch_programs", return_value=[summer_program()]), \
                patch.object(bonus, "_bill_detail", side_effect=detail):
            audit = {}
            frames = bonus._calc_month_frames(Mock(), cfg, first, last, True, {}, "test", audit,
                                             verbose=False, detail_config={})
        bonus._BILL_CACHE.clear()
        return frames, audit

    def test_accumulates_from_programme_start_and_pays_only_at_the_end(self):
        def month(day, amount):
            return bill([{"ma_sp": "230100008", "ten_dvt": "Két", "ma_dvt": "Két", "so_luong": 10,
                          "thanh_tien": amount, "gia_truoc_vat": amount / 10, "ngay_giao_hang": day,
                          "ngay_dat": day[:10] + " 08:00:00", "ma_phieu": f"DH{day[5:7]}"}])
        months = {"2026-07": month("2026-07-10T03:00:00.000Z", 6_000_000),
                  "2026-08": month("2026-08-10T03:00:00.000Z", 3_000_000),
                  "2026-09": month("2026-09-10T03:00:00.000Z", 3_000_000)}
        frames, audit = self._frames(date(2026, 8, 1), date(2026, 8, 31), months)
        self.assertEqual(audit["cumulative_missing_months"], ["2026-04", "2026-05", "2026-06"])
        self.assertEqual(audit["eligible_rows"], 0)  # 9,000,000 so far, and the programme is still running
        frames, audit = self._frames(date(2026, 9, 1), date(2026, 9, 30), months)
        self.assertEqual((audit["reached_rows"], audit["eligible_rows"]), (1, 0))
        self.assertIn("Can_xem", frames)
        # Missing masters are unknown, not empty months. Explicit empty masters prove
        # zero sales for April-June and make the whole-period calculation complete.
        months.update({f"2026-{m:02}": bill([]) for m in (4, 5, 6)})
        frames, audit = self._frames(date(2026, 9, 1), date(2026, 9, 30), months)
        self.assertEqual((audit["reached_rows"], audit["eligible_rows"]), (1, 1))
        gift = frames["BaoCao"][frames["BaoCao"]["Mã Đơn hàng"] == "TRẢ THƯỞNG"].iloc[0]
        self.assertEqual((gift["Sản phẩm Tặng"], gift["Số lượng Khuyến mãi"]), ("TIEN", 0))
        self.assertEqual(gift["Tiền mặt trả thưởng (đ)"], 550_000)
        self.assertEqual(gift["Mã CTKM"], "248/TB/GT/04/2026 - Loại A")
        issues = frames["CanBoSung"]
        gift_issues = issues[issues["Mã Đơn hàng"] == "TRẢ THƯỞNG"]["Trường"].tolist()
        self.assertFalse({"Brand", "Package", "Số lượng Khuyến mãi"} & set(gift_issues))

    def test_provisional_marks_reached_rows_before_the_end(self):
        months = {"2026-08": bill([{"ma_sp": "230100008", "ten_dvt": "Két", "ma_dvt": "Két", "so_luong": 10,
                                    "thanh_tien": 12_000_000, "ngay_giao_hang": "2026-08-10T03:00:00.000Z"}])}
        frames, audit = self._frames(date(2026, 8, 1), date(2026, 8, 31), months)
        self.assertEqual((audit["reached_rows"], audit["eligible_rows"]), (1, 0))
        self.assertFalse((frames["BaoCao"]["Mã Đơn hàng"] == "TRẢ THƯỞNG").any())

    def test_first_month_of_cumulative_programme_is_also_provisional(self):
        months = {"2026-04": bill([{"ma_sp": "230100008", "ten_dvt": "Két", "ma_dvt": "Két",
                                    "so_luong": 10, "thanh_tien": 12_000_000,
                                    "ngay_giao_hang": "2026-04-10T03:00:00.000Z"}])}
        frames, audit = self._frames(date(2026, 4, 1), date(2026, 4, 30), months)
        self.assertEqual((audit["reached_rows"], audit["eligible_rows"]), (1, 0))
        self.assertIn("Tạm tính", frames["Tong_hop"].to_string())
        self.assertFalse((frames["BaoCao"]["Mã Đơn hàng"] == "TRẢ THƯỞNG").any())


class ProgrammeAutomationTests(unittest.TestCase):
    def test_letter_levels_keep_separate_business_codes(self):
        self.assertEqual(calc.bonus_code("559/TB/GT/10/2026 – MỨC A - CT TẾT"),
                         "559/TB/GT/10/2026 - Mức A")
        self.assertEqual(calc.bonus_code("559/TB/GT/10/2026 – MỨC B - CT TẾT"),
                         "559/TB/GT/10/2026 - Mức B")

    def test_undeclared_method_keeps_progress_without_approved_rewards(self):
        program = summer_program()
        program["name"] = "570/TB/GT/10/2026_CT nhiều tháng"
        program["startDate"] = int(pd.Timestamp("2026-10-01", tz="Asia/Ho_Chi_Minh").timestamp() * 1000)
        program["endDate"] = int(pd.Timestamp("2026-12-31", tz="Asia/Ho_Chi_Minh").timestamp() * 1000)
        detail = bill([{"ma_sp": "230100008", "ten_dvt": "Két", "ma_dvt": "Két",
                        "so_luong": 10, "thanh_tien": 12_000_000}])
        with patch.object(bonus, "fetch_programs", return_value=[program]), \
                patch.object(bonus, "_bill_detail", return_value=detail):
            audit = {}
            frames = bonus._calc_month_frames(Mock(), bonus.load_config(), OCT1, OCT31, True, {},
                                             "test", audit, verbose=False, detail_config={})
        self.assertEqual((audit["reached_rows"], audit["eligible_rows"], audit["programs_need_method"]), (1, 0, 1))
        self.assertIn("Can_xem", frames)
        self.assertEqual(audit["quality_status"], "needs_review")
        self.assertFalse((frames["BaoCao"]["Mã Đơn hàng"] == "TRẢ THƯỞNG").any())

    def test_calculation_method_resolution(self):
        cfg = bonus.load_config()
        summer = summer_program()
        self.assertEqual(bonus._program_mode(summer, cfg), "cumulative")  # configured prefix
        monthly_q4 = {**summer, "name": "009/TB/GT/01/2026_Q4_CT TÍCH LŨY EATERY THEO THÁNG - MỨC 4"}
        self.assertEqual(bonus._program_mode(monthly_q4, cfg), "monthly")
        unknown = {**summer, "name": "570/TB/GT/10/2026_CHƯƠNG TRÌNH TÍCH LŨY SÂN CHƠI"}
        self.assertEqual(bonus._program_mode(unknown, cfg), "")
        declared = {"570/TB/GT/10/2026": {"Cách tính": "Tích lũy cả kỳ"}}
        self.assertEqual(bonus._program_mode(unknown, cfg, declared), "cumulative")
        self.assertEqual(bonus._program_mode(summer, cfg, {"248/TB/GT/04/2026": {"Cách tính": "theo thang"}}),
                         "monthly")
        rows = bonus._program_todo_rows([unknown], [{"Chương trình": "999/TB/GT/10/2026_X - MỨC 1",
                                                      "Vấn đề": "unsupported type"}])
        self.assertEqual([r["Mã CT"] for r in rows], ["570/TB/GT/10/2026", "999/TB/GT/10/2026 - Mức 1"])
        self.assertEqual(rows[0]["Thời gian"], "01/04/2026 - 30/09/2026")

    def test_quota_goes_to_the_customer_who_reached_first(self):
        program = qty_program(customers=(C1, C2))
        program["soSuat"] = "1"
        detail = bill([{"so_luong": 80, "ngay_giao_hang": "2026-10-20T03:00:00.000Z"},
                       {"so_luong": 80, "ID_khachhang": C2, "ma_kh": "KH2",
                        "ngay_giao_hang": "2026-10-05T03:00:00.000Z"}])
        with patch.dict(os.environ, {"PROMOTION_BONUS_STTT": "0"}), \
                patch.object(bonus, "fetch_programs", return_value=[program]), \
                patch.object(bonus, "_bill_detail", return_value=detail):
            audit = {}
            frames = bonus._calc_month_frames(Mock(), bonus.load_config(), OCT1, OCT31, True, {}, "test", audit,
                                             verbose=False, detail_config={})
        self.assertEqual(audit["over_quota"], ["581/TB/GT/10/2026: 1/1, hết suất 1 khách"])
        self.assertEqual(audit["quota"]["581/TB/GT/10/2026"]["cut"], 1)
        self.assertTrue(frames["Kiem_tra"].astype(str).apply(lambda c: c.str.contains("Hết suất")).any().any())
        report = frames.get("BaoCao")
        if report is not None and "Mã Khách hàng" in report:
            gifts = report[report["Mã Đơn hàng"] == calc.GIFT_ORDER]
            self.assertEqual(set(gifts["Mã Khách hàng"]), {"KH2"})  # C2 reached on 05/10, before C1

    def test_monthly_programme_counts_slots_paid_in_earlier_months(self):
        program = qty_program(customers=(C1, C2))
        program.update(soSuat="1", name="581/TB/GT/09/2026_CT THEO THÁNG",
                       startDate=int(pd.Timestamp("2026-09-01", tz="Asia/Ho_Chi_Minh").timestamp() * 1000),
                       endDate=int(pd.Timestamp("2026-10-31", tz="Asia/Ho_Chi_Minh").timestamp() * 1000))
        months = {9: bill([{"so_luong": 80, "ngay_giao_hang": "2026-09-10T03:00:00.000Z"}]),
                  10: bill([{"so_luong": 80, "ID_khachhang": C2, "ma_kh": "KH2",
                             "ngay_giao_hang": "2026-10-05T03:00:00.000Z"}])}
        bonus._BILL_CACHE.clear()
        with patch.dict(os.environ, {"PROMOTION_BONUS_STTT": "0"}), \
                patch.object(bonus, "fetch_programs", return_value=[program]), \
                patch.object(bonus, "_bill_detail", side_effect=lambda first, *a: months[first.month]):
            audit = {}
            bonus._calc_month_frames(Mock(), bonus.load_config(), OCT1, OCT31, True, {}, "test", audit,
                                     verbose=False, detail_config={})
        bonus._BILL_CACHE.clear()
        quota = audit["quota"]["581/TB/GT/09/2026"]
        self.assertEqual((quota["used_before"], quota["paid"], quota["cut"]), (1, 0, 1))

    def test_quota_rules(self):
        self.assertEqual(calc.program_quota({"soSuat": "327"}), 327)
        self.assertEqual(calc.program_quota({"soSuat": ""}), 0)
        self.assertEqual(calc.program_quota({"soSuat": "1", "gioiHanCT": False}), 0)
        program = qty_program(customers=(C1, C2), multiple=True)
        detail = bill([{"so_luong": 216, "ngay_giao_hang": "2026-10-02T03:00:00.000Z"},
                       {"so_luong": 72, "ID_khachhang": C2, "ma_kh": "KH2",
                        "ngay_giao_hang": "2026-10-03T03:00:00.000Z"}])
        results, _ = calc.compute([program], calc.sold_lines(detail, OCT1, OCT31), {})
        stats = calc.apply_quota(results[0], 4, used_before=2)
        rows = {r["ma"]: r for r in results[0].rows}
        first, second = rows["KHHO112323"], rows["KH2"]
        self.assertEqual((stats["paid"], stats["reduced"], stats["cut"]), (2, 1, 1))
        self.assertEqual(first["_rewards"][0][3], 24)  # 3 multiples reduced to the 2 slots left
        self.assertEqual(second["_eligible"], calc.QUOTA_OUT)


class DisplayShapeTests(unittest.TestCase):
    def test_list_and_json_grading_payloads_are_read(self):
        records = [
            {"ma_kh": "K1", "ten_ct": "CTTB", "cham_diem": [{"tieu_chi": "Kệ", "ket_qua": "Đạt"}], "tt_cham_diem": 1},
            {"ma_kh": "K2", "ten_ct": "CTTB", "cham_diem": '{"ket_qua": "Không đạt"}', "tt_cham_diem": 2},
            {"ma_kh": "K3", "ten_ct": "CTTB", "cham_diem": None, "tt_cham_diem": None},
        ]
        passes = calc.display_passes(records)
        self.assertEqual(passes[("K1", "cttb")], "Đạt")
        self.assertEqual(passes[("K2", "cttb")], "Không đạt")
        self.assertEqual(passes[("K3", "cttb")], "Đã ghi nhận, chưa chấm")
        summary = calc.display_summary(records, [{"cttb": {"ten": "CTTB"}}])
        self.assertIn("[].ket_qua:str", summary["grading_keys"])
        self.assertEqual(summary["status_by_grading"]["1|Đạt"], 1)
        self.assertEqual(summary["status_by_grading"]["None|rỗng"], 1)


class DisplayByProgrammeTests(unittest.TestCase):
    def test_gradings_are_requested_per_display_programme_over_its_period(self):
        calls = []

        def get_json(url, params, **kwargs):
            calls.append(dict(params))
            if params.get("ten_cttb") == "CTTB PET":
                return {"data": [{"ma_kh": "K1", "ten_ct": "CTTB PET", "cham_diem": {"chon_1": "Đạt"}}]}
            if params.get("ten_cttb"):
                raise RuntimeError("rejected")
            return {"data": [{"ma_kh": "K1", "ten_ct": "CTTB PET", "cham_diem": {}}]}

        client = Mock(get_json=get_json)
        start_ms = int(pd.Timestamp("2026-07-01", tz="Asia/Ho_Chi_Minh").timestamp() * 1000)
        programs = [{"cttb": {"ten": "CTTB PET"}, "startDate": start_ms},
                    {"cttb": {"ten": "CTTB LẠ"}}, {"cttb": None}]
        records, stats = calc.fetch_display_for_programs(client, programs, OCT1, OCT31)
        self.assertEqual((stats["programmes"], stats["graded"], stats["errors"]), (2, 1, 1))
        pet = next(c for c in calls if c.get("ten_cttb") == "CTTB PET")
        self.assertEqual((pet["tu_ngay"], pet["den_ngay"]), ("01/07/2026", "31/10/2026"))
        self.assertEqual(calc.display_passes(records)[("K1", "cttb pet")], "Đạt")


class DisplayOverrideTests(unittest.TestCase):
    def run_month(self, overrides):
        program = qty_program(customers=(C1, C2), cttb={"ten": "CTTB PET", "ket_qua": {"label": "Đạt"}})
        detail = bill([{"so_luong": 80}, {"so_luong": 80, "ID_khachhang": C2, "ma_kh": "KH2"}])
        with patch.dict(os.environ, {"PROMOTION_BONUS_STTT": "0"}), \
                patch.object(bonus, "fetch_programs", return_value=[program]), \
                patch.object(bonus, "_bill_detail", return_value=detail), \
                patch.object(calc, "fetch_display_records", return_value=[]):
            audit = {}
            bonus.TODO_UPDATES.clear()
            with patch.dict(os.environ, {"BOSUNG_MAPPING": "true"}):
                frames = bonus._calc_month_frames(Mock(), bonus.load_config(), OCT1, OCT31, True, {}, "t", audit,
                                                  verbose=False, detail_config={"display_overrides": overrides,
                                                                                "bosung_mapping": True})
        return frames, audit

    def test_pending_display_customers_are_listed_and_manual_results_apply(self):
        frames, audit = self.run_month({})
        self.assertEqual(audit["review_display_rows"], 2)
        frames, audit = self.run_month({"KHHO112323|cttb pet": "Đạt", "KH2|cttb pet": "Không đạt"})
        self.assertEqual((audit["eligible_rows"], audit["review_display_rows"]), (1, 0))
        ledger = frames["TraThuong"]
        self.assertIn(calc.DISPLAY_FAILED, set(ledger["Trạng thái"]))
        todo = [r for r in bonus.TODO_UPDATES["TraThuong 2026-10"] if r["_sheet"] == "TrungBay"]
        self.assertEqual(todo, [])

    def test_waiting_customers_go_to_the_display_sheet(self):
        self.run_month({})
        todo = [r for r in bonus.TODO_UPDATES["TraThuong 2026-10"] if r["_sheet"] == "TrungBay"]
        self.assertEqual({r["Mã Khách hàng"] for r in todo}, {"KHHO112323", "KH2"})
        self.assertEqual({r["Chương trình trưng bày"] for r in todo}, {"CTTB PET"})
