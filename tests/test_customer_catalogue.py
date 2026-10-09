from __future__ import annotations

import unittest
from unittest.mock import Mock

import pandas as pd

from customer_catalogue import enrich_customer_config
from promotion_detail import build_report
from test_promotion_detail import config, source


def customer(**changes):
    row = {"ID": "customer-id", "makh": "KH01", "loai_kh": "1b",
           "tinh_thanh_moi": "Khánh Hòa", "code_router": "R01", "tenkh": "Customer",
           "dia_chi": "Address", "sdt": "0123456789"}
    row.update(changes)
    return row


class CustomerCatalogueTests(unittest.TestCase):
    def test_contact_person_uses_observed_customer_field_and_never_customer_name(self):
        rows = [customer(nguoi_lien_he="Contact person"),
                customer(ID="missing", makh="KH02"),
                customer(ID="structured", makh="KH03", nguoi_lien_he=["ambiguous"])]
        cfg = enrich_customer_config(self.client([{"total": 3, "data": rows}]), {})
        self.assertEqual(cfg["customer_catalogue"]["customer-id"]["Tên người liên hệ"], "Contact person")
        self.assertEqual(cfg["customer_catalogue"]["missing"]["Tên người liên hệ"], "")
        self.assertEqual(cfg["customer_catalogue"]["structured"]["Tên người liên hệ"], "")
        self.assertEqual(cfg["customer_catalogue_audit"]["structured_fields_not_mapped"]["nguoi_lien_he"], 1)

    def client(self, payloads):
        client = Mock()
        # Second creation-date window (today) is empty unless a test says otherwise.
        client.get_json.side_effect = list(payloads) + [{"total": 0, "data": []}] * 3
        return client

    def test_complete_catalogue_joins_by_id_and_keeps_transaction_values(self):
        client = self.client([{"total": 1, "data": [customer()]}])
        cfg = config()
        cfg["customers"] = {}
        result = enrich_customer_config(client, cfg)
        self.assertNotIn("customer_catalogue", cfg)
        report, issues = build_report(pd.DataFrame([source(ten_kh="Order name")]), result)
        self.assertEqual(report.iloc[0]["Tỉnh"], "Khánh Hòa")
        self.assertEqual(report.iloc[0]["Loại KH"], "1b")
        self.assertEqual(report.iloc[0]["Route"], "R01")
        self.assertEqual(report.iloc[0]["Tên Khách hàng"], "Order name")
        self.assertEqual(report.iloc[0]["Số ĐT"], "0123456789")
        self.assertNotIn("Tỉnh", issues["Trường"].tolist())
        params = client.get_json.call_args_list[0].args[1]
        self.assertEqual(params["tu_ngay"], "01/01/1900")
        today_params = client.get_json.call_args_list[-1].args[1]
        self.assertEqual(today_params["tu_ngay"], today_params["den_ngay"])
        self.assertEqual(params["kieu_ngay"], "cdate")
        for name in ("status", "nhan_vien", "phong_ban_nv", "loai_kh"):
            self.assertNotIn(name, params)
        self.assertNotIn("Customer", str(result["customer_catalogue_audit"]))

    def test_explicit_mapping_has_precedence(self):
        cfg = enrich_customer_config(self.client([{"data": [customer()]}, {"data": []}]), config())
        report, issues = build_report(pd.DataFrame([source()]), cfg)
        self.assertEqual(report.iloc[0]["Tỉnh"], "KHÁNH HÒA")
        self.assertTrue(issues.empty)

    def test_wrong_id_does_not_join_by_code(self):
        cfg = config()
        cfg["customers"] = {}
        cfg = enrich_customer_config(self.client([{"total": 1, "data": [customer(ID="other-id")]}]), cfg)
        report, issues = build_report(pd.DataFrame([source()]), cfg)
        self.assertTrue(pd.isna(report.iloc[0]["Tỉnh"]))
        self.assertIn("Tỉnh", issues["Trường"].tolist())

    def test_changed_code_joins_by_id_and_keeps_order_code(self):
        cfg = config()
        cfg["customers"] = {}
        cfg = enrich_customer_config(self.client([{"total": 1, "data": [customer(makh="CHANGED")]}]), cfg)
        report, issues = build_report(pd.DataFrame([source()]), cfg)
        self.assertFalse(pd.isna(report.iloc[0]["Tỉnh"]))
        self.assertNotEqual(report.iloc[0]["Mã Khách hàng"], "CHANGED")
        self.assertNotIn("Mã Khách hàng", issues["Trường"].tolist() if not issues.empty else [])

    def test_no_total_short_page_is_not_end(self):
        client = self.client([{"data": [customer()]},
                              {"data": [customer(ID="other", makh="KH02")]}, {"data": []}])
        cfg = enrich_customer_config(client, config())
        self.assertEqual(cfg["customer_catalogue_audit"]["count"], 2)
        self.assertEqual(client.get_json.call_count, 4)  # 3 historical pages + today window

    def test_invalid_catalogue_rejected_before_enrichment(self):
        cases = [
            [{"data": [customer()]}, {"data": [customer()]}],
            [{"total": 2, "data": [customer(), customer(loai_kh="other")]}],
            [{"total": 2, "data": [customer()]}, {"total": 3, "data": []}],
            [{"total": 2, "data": [customer()]}, {"total": 2, "data": []}],
            [{"data": [customer(ID=["unexpected"])]}],
            [{"data": None}],
        ]
        for payloads in cases:
            with self.subTest(payloads=payloads), self.assertRaises((ValueError, TypeError)):
                enrich_customer_config(self.client(payloads), config(), attempts=1)

    def test_customer_created_in_both_windows_is_rejected(self):
        client = Mock()
        client.get_json.side_effect = [{"total": 1, "data": [customer()]},
                                       {"total": 1, "data": [customer()]}]
        with self.assertRaises(ValueError):
            enrich_customer_config(client, config())

    def test_live_total_change_is_retried_then_succeeds(self):
        payloads = [{"total": 2, "data": [customer()]}, {"total": 3, "data": []},
                    {"total": 1, "data": [customer()]}]
        cfg = enrich_customer_config(self.client(payloads), config())
        self.assertEqual(list(cfg["customer_catalogue"]), ["customer-id"])

    def test_missing_id_reported_without_code_fallback(self):
        cfg = enrich_customer_config(self.client([{"total": 1, "data": [customer(ID=None)]}]), config())
        self.assertEqual(cfg["customer_catalogue"], {})
        self.assertEqual(cfg["customer_catalogue_audit"]["unkeyed_rows"], 1)

    def test_structured_routes_are_not_guessed_and_other_metadata_is_kept(self):
        cfg = enrich_customer_config(self.client([{"total": 1, "data": [
            customer(code_router=["R1", "R2"])]}]), config())
        self.assertEqual(cfg["customer_catalogue"]["customer-id"]["Route"], "")
        self.assertEqual(cfg["customer_catalogue"]["customer-id"]["Loại KH"], "1b")
        self.assertEqual(cfg["customer_catalogue_audit"]["structured_fields_not_mapped"], {"code_router": 1})
        report, _ = build_report(pd.DataFrame([source(tuyen_code="ORDER_ROUTE")]), cfg)
        self.assertEqual(report.iloc[0]["Route"], "ORDER_ROUTE")

    def test_invalid_start_date_fails_before_request(self):
        client = self.client([])
        with self.assertRaises(ValueError):
            enrich_customer_config(client, {"customer_catalogue_start_date": "wrong"})
        client.get_json.assert_not_called()

    def test_legacy_province_fills_only_a_missing_current_province(self):
        rows = [customer(tinh_thanh_moi="", tinhthanh_pho="Hà Nội"),
                customer(ID="other", tinh_thanh_moi="Khánh Hòa", tinhthanh_pho="Phú Yên")]
        cfg = enrich_customer_config(self.client([{"total": 2, "data": rows}]), {})
        legacy = cfg["customer_catalogue"]["customer-id"]
        self.assertEqual((legacy["Tỉnh"], legacy["_province_source"]), ("Hà Nội", "tinhthanh_pho"))
        self.assertEqual(cfg["customer_catalogue"]["other"]["Tỉnh"], "Khánh Hòa")

    def test_structured_legacy_province_is_not_used_as_text(self):
        cfg = enrich_customer_config(self.client([{"total": 1, "data": [
            customer(tinh_thanh_moi="", tinhthanh_pho=["Hà Nội"])]}]), {})
        self.assertEqual(cfg["customer_catalogue"]["customer-id"]["Tỉnh"], "")

    def test_province_uses_exact_dms_address_suffix_and_keeps_its_source(self):
        rows = [customer(tinh_thanh_moi='', dia_chi='Customer address, PHÚ YÊN'),
                customer(ID='middle', tinh_thanh_moi='', dia_chi='Phú Yên, unknown suffix'),
                customer(ID='current', tinh_thanh_moi='Đắk Lắk', dia_chi='Customer address, PHÚ YÊN')]
        cfg = enrich_customer_config(self.client([{'total': 3, 'data': rows}]),
                                     {'customer_address_provinces': {'Phú Yên': 'Phú Yên'}})
        self.assertEqual(cfg['customer_catalogue']['customer-id']['Tỉnh'], 'Phú Yên')
        self.assertIn('dia_chi', cfg['customer_catalogue']['customer-id']['_province_source'])
        self.assertEqual(cfg['customer_catalogue']['middle']['Tỉnh'], '')
        self.assertEqual(cfg['customer_catalogue']['current']['Tỉnh'], 'Đắk Lắk')
        self.assertEqual(sum(cfg['customer_catalogue_audit']['province_fallback_sources'].values()), 1)
