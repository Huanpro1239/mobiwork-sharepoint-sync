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
    def client(self, payloads):
        client = Mock()
        client.get_json.side_effect = payloads
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
        params = client.get_json.call_args.args[1]
        self.assertEqual(params["tu_ngay"], "01/01/1900")
        self.assertEqual(params["kieu_ngay"], "cdate")
        for name in ("status", "nhan_vien", "phong_ban_nv", "loai_kh"):
            self.assertNotIn(name, params)
        self.assertNotIn("Customer", str(result["customer_catalogue_audit"]))

    def test_explicit_mapping_has_precedence(self):
        cfg = enrich_customer_config(self.client([{"data": [customer()]}, {"data": []}]), config())
        report, issues = build_report(pd.DataFrame([source()]), cfg)
        self.assertEqual(report.iloc[0]["Tỉnh"], "KHÁNH HÒA")
        self.assertTrue(issues.empty)

    def test_wrong_id_or_changed_code_does_not_join_by_code(self):
        for api in [customer(ID="other-id"), customer(makh="CHANGED")]:
            cfg = config()
            cfg["customers"] = {}
            cfg = enrich_customer_config(self.client([{"total": 1, "data": [api]}]), cfg)
            report, issues = build_report(pd.DataFrame([source()]), cfg)
            self.assertTrue(pd.isna(report.iloc[0]["Tỉnh"]))
            self.assertIn("Tỉnh", issues["Trường"].tolist())

    def test_no_total_short_page_is_not_end(self):
        client = self.client([{"data": [customer()]},
                              {"data": [customer(ID="other", makh="KH02")]}, {"data": []}])
        cfg = enrich_customer_config(client, config())
        self.assertEqual(cfg["customer_catalogue_audit"]["count"], 2)
        self.assertEqual(client.get_json.call_count, 3)

    def test_invalid_catalogue_rejected_before_enrichment(self):
        cases = [
            [{"data": [customer()]}, {"data": [customer()]}],
            [{"total": 2, "data": [customer(), customer(loai_kh="other")]}],
            [{"total": 2, "data": [customer()]}, {"total": 3, "data": []}],
            [{"total": 2, "data": [customer()]}, {"total": 2, "data": []}],
            [{"data": [customer(loai_kh=["unexpected"])]}],
            [{"data": None}],
        ]
        for payloads in cases:
            with self.subTest(payloads=payloads), self.assertRaises((ValueError, TypeError)):
                enrich_customer_config(self.client(payloads), config())

    def test_missing_id_reported_without_code_fallback(self):
        cfg = enrich_customer_config(self.client([{"total": 1, "data": [customer(ID=None)]}]), config())
        self.assertEqual(cfg["customer_catalogue"], {})
        self.assertEqual(cfg["customer_catalogue_audit"]["unkeyed_rows"], 1)

    def test_invalid_start_date_fails_before_request(self):
        client = self.client([])
        with self.assertRaises(ValueError):
            enrich_customer_config(client, {"customer_catalogue_start_date": "wrong"})
        client.get_json.assert_not_called()
