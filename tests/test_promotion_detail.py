from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

import promotion_detail as module


def source(**changes):
    row = dict(ma_phieu='ORDER1', stt=1, ma_sp='SKU1', ten_sp='Product', so_luong=10,
               gia_truoc_vat=100, vat=8, is_km=False, loai_hang='Bán hàng',
               ma_kh='KH01', ID_khachhang='customer-id', ma_nv_dat='NV01',
               ngay_ban_hang='2026-10-05', ten_dvt='Thùng',
               promotion=json.dumps([dict(id='p1', ten_khuyen_mai='003/TB/GT/01/2026_Q3')]))
    row.update(changes)
    return row


def config():
    return dict(publish_enabled=False,
                employees={'NV01': {'Vùng': 'Miền Trung', 'SS Code': 'SS01', 'SS Name': 'Supervisor'}},
                customers={'customer-id': {'Tỉnh': 'KHÁNH HÒA', 'DB Code': 'DB01', 'Tên NPP': 'NPP', 'Loại KH': '1b'}},
                products={'SKU1': {'Brand': 'Brand', 'Package': '1 way'}}, unit_conversions={})


class PromotionDetailTests(unittest.TestCase):
    def test_sale_pre_vat_and_gift_are_separate_without_inflation(self):
        gift = source(stt=2, is_km=True, loai_hang='Khuyến mãi', so_luong=2,
                      ctkm='003/TB/GT/01/2026_Q3')
        report, issues = module.build_report(pd.DataFrame([source(), gift]), config())
        self.assertTrue(issues.empty)
        self.assertEqual(list(report.columns), module.COLUMNS)
        self.assertEqual(report['Số lượng SELL-OUT'].sum(), 10)
        self.assertEqual(report['THÀNH TIỀN'].sum(), 1000)
        self.assertEqual(report['Số lượng Khuyến mãi'].sum(), 2)
        self.assertTrue(pd.isna(report.iloc[1]['THÀNH TIỀN']))
        self.assertEqual(report.iloc[0]['Mã CTKM'], '003/TB/GT/01/2026_Q3')

    def test_multiple_programs_do_not_duplicate_sales(self):
        rows = [source(), source(stt=2, is_km=True, loai_hang='Khuyến mãi', ctkm='CT1', so_luong=2),
                source(stt=3, is_km=True, loai_hang='Khuyến mãi', ctkm='CT2', so_luong=3)]
        report, _ = module.build_report(pd.DataFrame(rows), config())
        self.assertEqual(len(report), 3)
        self.assertEqual(report['Số lượng SELL-OUT'].sum(), 10)
        self.assertEqual(report['Số lượng Khuyến mãi'].sum(), 5)
        self.assertIn('CT1; CT2', report.iloc[0]['Mã CTKM'])
        self.assertEqual(report.iloc[2]['Mã CTKM'], 'CT2')

    def test_duplicate_and_missing_keys_fail(self):
        for rows in ([source(), source()], [source(stt=None)], [source(ma_phieu=None)]):
            with self.subTest(), self.assertRaises(ValueError):
                module.build_report(pd.DataFrame(rows), config())

    def test_unit_conversion_and_money_uses_original_quantity(self):
        cfg = config()
        cfg['unit_conversions'] = {'SKU1|Chai': {'target_unit': 'Thùng', 'factor': '0.04166666666666666666666666667'}}
        report, issues = module.build_report(pd.DataFrame([source(so_luong=24, ten_dvt='Chai')]), cfg)
        self.assertTrue(issues.empty)
        self.assertAlmostEqual(report.iloc[0]['Số lượng SELL-OUT'], 1)
        self.assertEqual(report.iloc[0]['THÀNH TIỀN'], 2400)

    def test_missing_units_and_price_are_not_zero(self):
        report, issues = module.build_report(pd.DataFrame([source(ten_dvt='Chai', gia_truoc_vat=None)]), config())
        self.assertTrue(pd.isna(report.iloc[0]['THÀNH TIỀN']))
        self.assertTrue(pd.isna(report.iloc[0]['Số lượng SELL-OUT']))
        self.assertEqual(len(issues), 2)

    def test_missing_dimensions_have_explicit_issues(self):
        _, issues = module.build_report(pd.DataFrame([source()]), {})
        self.assertIn('SS Code', issues['Trường'].tolist())
        self.assertIn('Brand', issues['Trường'].tolist())

    def test_no_program_orders_excluded(self):
        report, issues = module.build_report(pd.DataFrame([source(promotion='[]')]), config())
        self.assertTrue(report.empty)
        self.assertTrue(issues.empty)

    def test_python_and_json_promotion_serialization(self):
        self.assertEqual(module.promotion_list("[{'id': 'x'}]"), [{'id': 'x'}])
        self.assertEqual(module.promotion_list([{'id': 'x'}]), [{'id': 'x'}])
        with self.assertRaises(ValueError):
            module.promotion_list('{}')

    def test_conflicting_or_unknown_gift_flags_fail(self):
        for row in (source(is_km=False, loai_hang='Khuyến mãi'), source(is_km='maybe')):
            with self.assertRaises(ValueError):
                module.build_report(pd.DataFrame([row]), config())

    def test_program_code_mapping_preserves_suffix(self):
        cfg = config()
        cfg['program_codes'] = {'003/TB/GT/01/2026_Q3': '003/TB/GT/01/2026_Q3'}
        report, _ = module.build_report(pd.DataFrame([source()]), cfg)
        self.assertEqual(report.iloc[0]['Mã CTKM'], '003/TB/GT/01/2026_Q3')

    def test_empty_source_returns_template_columns(self):
        report, _ = module.build_report(pd.DataFrame(), config())
        self.assertEqual(list(report.columns), module.COLUMNS)

    def test_run_dry_and_production_guards(self):
        from datetime import date
        from io import BytesIO
        from mobiwork import ReportConfig
        for dry, missing, enabled in [(True, False, False), (True, True, True),
                                      (False, False, True), (False, True, True),
                                      (False, True, False)]:
            with self.subTest(dry=dry, missing=missing, enabled=enabled), tempfile.TemporaryDirectory() as tmp:
                cfg = config()
                cfg['publish_enabled'] = enabled
                if missing:
                    cfg['products'] = {}
                config_path = Path(tmp) / 'config.json'
                config_path.write_text(json.dumps(cfg), encoding='utf-8')
                buffer = BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    pd.DataFrame([source()]).to_excel(writer, sheet_name='ChiTietSP', index=False)
                sp = Mock()
                sp.download_file_bytes.return_value = buffer.getvalue()
                sp.upload_file.return_value = {'upload_skipped': True}
                bill = ReportConfig(key='bill', enabled=True, name='DonBanHang', folder='04_DonBanHang', export_mode='order')
                # Path redirection keeps all runtime output confined to a temporary directory.
                actual_path = Path
                output = Path(tmp) / 'output'
                output.mkdir()
                (output / 'DonBanHang_2026-10.xlsx').write_bytes(buffer.getvalue())
                with patch.object(module, 'CONFIG', config_path), \
                     patch.object(module, 'Path', side_effect=lambda value, actual_path=actual_path, tmp=tmp: actual_path(tmp) / value), \
                     patch.object(module, 'load_reports', return_value=[bill]), \
                     patch.object(module, 'incremental_target_dates', return_value=[date(2026, 10, 5)]), \
                     patch.object(module, 'write_workbook', return_value=actual_path('report.xlsx')), \
                     patch.object(module.SemanticSharePointClient, 'from_env', return_value=sp) as factory, \
                     patch.dict(os.environ, {'DRY_RUN': 'true' if dry else 'false', 'SHAREPOINT_DRIVE_ID': 'drive'}):
                    if not dry and missing and enabled:
                        with self.assertRaisesRegex(ValueError, 'Nothing published'):
                            module.run()
                    else:
                        result = module.run()
                        self.assertEqual(result['status'], 'needs_mapping' if missing else 'success')
                    if dry:
                        factory.assert_not_called()
                    if dry or missing or not enabled:
                        sp.upload_file.assert_not_called()
                    else:
                        sp.upload_file.assert_called_once()
                    manifest = json.loads((output / 'promotion_detail_manifest.json').read_text(encoding='utf-8'))
                    self.assertEqual(manifest['status'], 'failed' if not dry and missing and enabled else ('needs_mapping' if missing else 'success'))

class PromotionDetailConfigTests(unittest.TestCase):
    def test_invalid_config_cannot_enable_publish(self):
        for cfg in ([], {'publish_enabled': 'false'},
                    {'publish_enabled': True, 'employees': []},
                    {'publish_enabled': False, 'products': {'sku': 'bad'}},
                    {'publish_enabled': False, 'program_codes': {'name': ''}}):
            with self.subTest(cfg=cfg), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'config.json'
                path.write_text(json.dumps(cfg), encoding='utf-8')
                with patch.object(module, 'CONFIG', path), self.assertRaises(ValueError):
                    module.load_config()

class PromotionDetailLegacyTests(unittest.TestCase):
    def test_legacy_gift_fields_and_business_order_date(self):
        gift = source(stt=2, ma_sp=None, ten_sp=None, so_luong=None, ten_dvt=None,
                      ma_sp_km='SKU1', ten_sp_km='Gift', so_luong_km=3, ten_dvt_km='Thùng',
                      is_km=True, loai_hang='Khuyến mãi', ctkm='CT', ngay_dat='2026-07-09',
                      ngay_ban_hang=None)
        report, issues = module.build_report(pd.DataFrame([gift]), config())
        self.assertTrue(issues.empty)
        self.assertEqual(report.iloc[0]['Sản phẩm Tặng'], 'SKU1')
        self.assertEqual(report.iloc[0]['Số lượng Khuyến mãi'], 3)
        self.assertEqual(report.iloc[0]['Ngày Đơn hàng'].date().isoformat(), '2026-07-09')

class PromotionDetailReferenceTests(unittest.TestCase):
    def test_reference_customer_mapping_requires_employee_and_unique_identity(self):
        cfg = config()
        cfg['customers'] = {}
        cfg['customer_codes'] = {'KH01': {'employee_code': 'NV01', 'Tỉnh': 'Province',
                                         'DB Code': 'NPP1', 'Tên NPP': 'NPP', 'Loại KH': '1b'}}
        report, _ = module.build_report(pd.DataFrame([source()]), cfg)
        self.assertEqual(report.iloc[0]['DB Code'], 'NPP1')
        report, issues = module.build_report(pd.DataFrame([source(), source(stt=2, ID_khachhang='another-id')]), cfg)
        self.assertTrue(report['DB Code'].isna().all())
        self.assertIn('Mã Khách hàng', issues['Trường'].tolist())
        report, issues = module.build_report(pd.DataFrame([source(ma_nv_dat='other')]), cfg)
        self.assertTrue(pd.isna(report.iloc[0]['DB Code']))
        self.assertIn('DB Code', issues['Trường'].tolist())

class ProductCatalogueTests(unittest.TestCase):
    def test_catalogue_enriches_units_without_overwriting_reference(self):
        from test_promotion_bonus import FakeMobiWork
        client = FakeMobiWork([{'total': 2, 'data': [
            {'ma_sp': 'SKU1', 'nhan_hieu': 'Official', 'dvt_chan': 'Thùng', 'dvt_le': 'Chai', 'hsqd': 24},
            {'ma_sp': 'SKU2', 'nhan_hieu': 'Brand2', 'dvt_chan': 'Két', 'dvt_le': 'Chai', 'hsqd': 20}]}])
        cfg = module.enrich_product_config(client, config())
        self.assertEqual(cfg['products']['SKU1']['Brand'], 'Brand')
        self.assertEqual(cfg['products']['SKU2']['Brand'], 'Brand2')
        self.assertAlmostEqual(float(cfg['unit_conversions']['SKU1|Chai']['factor']), 1/24)
        self.assertNotIn('SKU1|Chai', config()['unit_conversions'])

    def test_catalogue_integrity_guards(self):
        from test_promotion_bonus import FakeMobiWork
        cases = [
            [{'total': 2, 'data': [{'ma_sp': 'a'}]}, {'total': 2, 'data': []}],
            [{'data': [{'ma_sp': 'a'}]}, {'data': [{'ma_sp': 'a'}]}],
            [{'total': 2, 'data': [{'ma_sp': 'a'}, {'ma_sp': 'a', 'nhan_hieu': 'other'}]}],
            [{'total': 2, 'data': [{'ma_sp': 'a'}]}, {'total': 3, 'data': [{'ma_sp': 'b'}]}],
        ]
        for payloads in cases:
            with self.subTest(payloads=payloads), self.assertRaises(ValueError):
                module.enrich_product_config(FakeMobiWork(payloads), config())
