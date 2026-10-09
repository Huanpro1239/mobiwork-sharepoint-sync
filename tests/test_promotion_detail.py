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
    def test_internal_program_ids_resolve_on_sale_and_gift(self):
        cfg = config()
        client = Mock()
        with patch.object(module, 'fetch_programs', return_value=[
            {'_id': 'internal-id', 'name': '003/TB/GT/01/2026_Q4_CT TÍCH LŨY'},
            {'_id': 'other-id', 'name': 'Chương trình miền Bắc'}]):
            cfg = module.enrich_program_config(client, cfg)
        sale = source(promotion='[{"id":"other-id"}]', ctkmFull_id='internal-id')
        gift = source(stt=2, promotion='[]', ctkmFull_id='internal-id',
                      is_km=True, loai_hang='Khuyến mãi')
        report, _ = module.build_report(pd.DataFrame([sale, gift]), cfg)
        self.assertEqual(report.iloc[0]['Mã CTKM'],
                         '003/TB/GT/01/2026_Q4; Chương trình miền Bắc')
        self.assertEqual(report.iloc[1]['Mã CTKM'], '003/TB/GT/01/2026_Q4')
        self.assertEqual(list(report.columns), module.COLUMNS)
        self.assertNotIn('internal-id', report.to_string())

    def test_template_does_not_restrict_regions_or_supply_business_metadata(self):
        cfg = module.load_config()
        self.assertEqual(cfg['employees'], {})
        self.assertEqual(cfg['customer_codes'], {})
        rows = [source(ma_phieu='NORTH', ma_nv_dat='HNI0101'),
                source(ma_phieu='SOUTH', ma_nv_dat='HCM0101'),
                source(ma_phieu='UNKNOWN', ma_nv_dat='UNKNOWN01')]
        report, issues = module.build_report(pd.DataFrame(rows), cfg)
        self.assertEqual(len(report), 3)
        self.assertEqual(report.iloc[0]['Vùng'], 'Miền Bắc')
        self.assertEqual(report.iloc[1]['Vùng'], 'Miền Nam')
        self.assertTrue(pd.isna(report.iloc[2]['Vùng']))
        self.assertTrue(report['SS Code'].isna().all())
        self.assertTrue(report['DB Code'].isna().all())
        self.assertIn('Vùng', issues['Trường'].tolist())

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
        for dry, missing, enabled, allow in [(True, False, False, False), (True, True, True, False),
                                             (False, False, True, False), (False, True, True, False),
                                             (False, True, False, False), (False, True, True, True),
                                             (True, True, True, True), (False, 'price', True, True)]:
            with self.subTest(dry=dry, missing=missing, enabled=enabled), tempfile.TemporaryDirectory() as tmp:
                cfg = config()
                cfg['publish_enabled'] = enabled
                cfg['allow_incomplete_publish'] = allow
                if missing:
                    cfg['products'] = {}
                config_path = Path(tmp) / 'config.json'
                config_path.write_text(json.dumps(cfg), encoding='utf-8')
                buffer = BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    pd.DataFrame([source(gia_truoc_vat=None) if missing == 'price' else source()]).to_excel(writer, sheet_name='ChiTietSP', index=False)
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
                     patch.object(module, 'write_detail_workbook', return_value=actual_path('report.xlsx')), \
                     patch.object(module.SemanticSharePointClient, 'from_env', return_value=sp) as factory, \
                     patch.dict(os.environ, {'DRY_RUN': 'true' if dry else 'false', 'SHAREPOINT_DRIVE_ID': 'drive',
                                             'ALLOW_INCOMPLETE_DETAIL': 'false',
                                             'PUBLISH_PROMOTION_DETAIL': 'false'}):
                    blocked = not dry and missing and enabled and (not allow or missing == 'price')
                    expected_status = ('published_with_issues' if missing and enabled and allow and not dry
                                       else 'needs_mapping' if missing else 'success')
                    if blocked:
                        with self.assertRaisesRegex(ValueError, 'Nothing published'):
                            module.run()
                    else:
                        result = module.run()
                        self.assertEqual(result['status'], expected_status)
                    if dry:
                        factory.assert_not_called()
                    if dry or blocked or not enabled:
                        sp.upload_file.assert_not_called()
                    else:
                        sp.upload_file.assert_called_once()
                    manifest = json.loads((output / 'promotion_detail_manifest.json').read_text(encoding='utf-8'))
                    self.assertEqual(manifest['status'], 'failed' if blocked else expected_status)
                    if not dry and enabled and not blocked:
                        self.assertTrue(manifest['results'][0]['workbook_published'])
                        self.assertIn('07_BaoCaoChiTietCTKM/2026/10/', manifest['results'][0]['remote_path'])

class PromotionDetailConfigTests(unittest.TestCase):
    def test_invalid_config_cannot_enable_publish(self):
        for cfg in ([], {'publish_enabled': 'false'},
                    {'publish_enabled': True, 'allow_incomplete_publish': 'true'},
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
    def test_accepted_blank_ss_keeps_the_sale_quantity(self):
        cfg = config()
        cfg['employees']['NV01'].pop('SS Code')
        cfg['employees']['NV01'].pop('SS Name')
        cfg['allow_blank_fields'] = ['SS Code', 'SS Name']
        report, issues = module.build_report(pd.DataFrame([source(so_luong=24)]), cfg)
        self.assertEqual(report.iloc[0]['Số lượng SELL-OUT'], 24)
        self.assertTrue(pd.isna(report.iloc[0]['SS Code']))
        self.assertTrue(issues.empty)

    def test_confirmed_sale_unit_is_scoped_to_exact_order_line_and_sku(self):
        row = source(ten_dvt='', ma_dvt='', ma_phieu='BH1', stt=1)
        cfg = {'line_unit_overrides': {'BH1|1|SKU1': {'unit': 'Chai', 'source': 'User confirmation'}}}
        self.assertEqual(module.source_unit(row, False, cfg), ('Chai', 'User confirmation'))
        for change in ({'stt': 2}, {'ma_phieu': 'BH2'}, {'ma_sp': 'OTHER'}):
            self.assertEqual(module.source_unit({**row, **change}, False, cfg), ('', ''))
        self.assertEqual(module.source_unit({**row, 'ten_dvt': 'Thùng'}, False, cfg), ('Thùng', 'ten_dvt'))
        cfg['line_unit_overrides']['BH1|1|SKU1'] = {'unit': 'Chai'}
        with self.assertRaises(ValueError):
            module.source_unit(row, False, cfg)

    def test_confirmed_sale_default_converts_new_orders_and_retains_its_evidence(self):
        cfg = config()
        cfg['sale_unit_defaults'] = {'SKU1': {'unit': 'Chai', 'source': 'Confirmed SKU default'}}
        cfg['unit_conversions']['SKU1|Chai'] = {'target_unit': 'Thùng', 'factor': str(1/24)}
        detail = pd.DataFrame([source(ma_phieu='NEW1', ten_dvt='', so_luong=24),
                               source(ma_phieu='NEW2', ten_dvt='', so_luong=48)])
        report, issues = module.build_report(detail, cfg)
        self.assertTrue(issues.empty)
        self.assertEqual(len(report), 2)
        for actual, expected in zip(report['Số lượng SELL-OUT'], (1, 2), strict=True):
            self.assertAlmostEqual(actual, expected)
        trace = module.unit_trace(detail, report, cfg)
        self.assertEqual(trace['Nguồn ĐVT'].tolist(), ['Confirmed SKU default'] * 2)

    def test_source_and_exact_line_units_take_priority_over_sale_defaults(self):
        row = source(ten_dvt='', ma_dvt='')
        cfg = {'sale_unit_defaults': {'SKU1': {'unit': 'Chai', 'source': 'Default'}},
               'line_unit_overrides': {'ORDER1|1|SKU1': {'unit': 'Thùng', 'source': 'Exact line'}}}
        self.assertEqual(module.source_unit(row, False, cfg), ('Thùng', 'Exact line'))
        self.assertEqual(module.source_unit({**row, 'ten_dvt': 'Két'}, False, cfg), ('Két', 'ten_dvt'))
        self.assertEqual(module.source_unit({**row, 'ma_dvt': 'Két'}, False, cfg), ('Két', 'ma_dvt'))
        self.assertEqual(module.source_unit({**row, 'ma_phieu': 'NEW'}, False, cfg), ('Chai', 'Default'))

    def test_sale_default_requires_evidence_and_does_not_supply_gift_or_other_sku_units(self):
        row = source(ten_dvt='', ma_dvt='')
        cfg = {'sale_unit_defaults': {'SKU1': {'unit': 'Chai', 'source': 'Default'}}}
        self.assertEqual(module.source_unit(row, True, cfg), ('', ''))
        self.assertEqual(module.source_unit({**row, 'ma_sp': 'OTHER'}, False, cfg), ('', ''))
        for invalid in ({'unit': 'Chai'}, {'unit': '', 'source': 'Default'}, 'Chai'):
            cfg['sale_unit_defaults']['SKU1'] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'Sale unit default'):
                module.source_unit(row, False, cfg)

    def test_missing_gift_unit_uses_only_its_linked_order_promotion(self):
        row = source(is_km=True, loai_hang='Khuyến mãi', ten_dvt='', ma_dvt='',
                     ctkm='Programme', ctkmFull_id='gift-id', so_luong=4,
                     promotion=[{'id': 'other-id', 'product': [{'ma_san_pham': 'SKU1',
                         'don_vi_tinh': {'viewData': 'Thùng'}}]},
                         {'id': 'gift-id', 'product': [{'ma_san_pham': 'SKU1',
                         'don_vi_tinh': {'viewData': 'Chai'}}]}])
        cfg = config()
        cfg['unit_conversions']['SKU1|Chai'] = {'target_unit': 'Thùng', 'factor': str(1/24)}
        report, issues = module.build_report(pd.DataFrame([row]), cfg)
        self.assertAlmostEqual(report.iloc[0]['Số lượng Khuyến mãi'], 4/24)
        self.assertTrue(issues.empty)
        trace = module.unit_trace(pd.DataFrame([row]), report, cfg)
        self.assertEqual(trace.iloc[0]['Nguồn ĐVT'], 'promotion.product.don_vi_tinh')
        self.assertEqual(module.source_unit({**row, 'ten_dvt': 'Thùng'}, True), ('Thùng', 'ten_dvt'))
        self.assertEqual(module.source_unit(row, False), ('', ''))
        self.assertEqual(module.source_unit({**row, 'ctkmFull_id': 'unlinked'}, True), ('', ''))
        row['promotion'][1]['product'].append({'ma_san_pham': 'SKU1',
                                               'don_vi_tinh': {'viewData': 'Két'}})
        self.assertEqual(module.source_unit(row, True), ('', ''))

    def test_dms_package_group_fills_known_packaging_only(self):
        from test_promotion_bonus import FakeMobiWork
        rows = [{'ma_sp': 'NEW', 'nganh_hang': '1 way'},
                {'ma_sp': 'SKU1', 'nganh_hang': '2 way'},
                {'ma_sp': 'GIFT', 'nganh_hang': ''},
                {'ma_sp': 'UNKNOWN', 'nganh_hang': 'Other'}]
        cfg = module.enrich_product_config(FakeMobiWork([{'total': 4, 'data': rows}]), config())
        self.assertEqual(cfg['products']['NEW']['Package'], '1 Way')
        self.assertEqual(cfg['products']['SKU1']['Package'], '1 way')
        self.assertNotIn('GIFT', cfg['products'])
        self.assertNotIn('UNKNOWN', cfg['products'])

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
        reference = config()
        reference['unit_conversions']['SKU1|Chai'] = {'target_unit': 'Két', 'factor': '0.05'}
        result = module.enrich_product_config(FakeMobiWork([{'total': 1, 'data': [
            {'ma_sp': 'SKU1', 'dvt_chan': 'Thùng', 'dvt_le': 'Chai', 'hsqd': 24}]}]), reference)
        self.assertEqual(result['unit_conversions']['SKU1|Chai'], reference['unit_conversions']['SKU1|Chai'])

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

class PromotionBusinessCodeTests(unittest.TestCase):
    def test_code_prefix_retains_quarter_suffix(self):
        self.assertEqual(module.program_code('003/TB/GT/01/2026_Q3', {}), '003/TB/GT/01/2026_Q3')
        self.assertEqual(module.program_code('003/TB/GT/01/2026_Q3_CHUONG_TRINH', {}), '003/TB/GT/01/2026_Q3')
        self.assertEqual(module.program_code('576/TB/GT/10/2026_CHUONG_TRINH', {}), '576/TB/GT/10/2026')
        self.assertEqual(module.program_code('custom_name', {}), 'custom_name')

class PromotionGiftUnitTests(unittest.TestCase):
    def test_actual_gift_glasses_remain_pieces(self):
        gift = source(is_km=True, loai_hang='Khuyến mãi', so_luong=9, ten_dvt='Cái', ctkm='CT')
        detail = pd.DataFrame([gift])
        report, issues = module.build_report(detail, config())
        self.assertTrue(issues.empty)
        self.assertEqual(report.iloc[0]['Số lượng Khuyến mãi'], 9)
        trace = module.unit_trace(detail, report, config())
        self.assertEqual(trace.iloc[0]['ĐVT báo cáo'], 'Cái')
        self.assertEqual(trace.iloc[0]['Hệ số'], 1)


class PromotionUnitGapTests(unittest.TestCase):
    def test_sold_piece_items_stay_in_pieces(self):
        report, issues = module.build_report(pd.DataFrame([source(ma_sp='530200025', ten_dvt='Cái', so_luong=3)]), config())
        self.assertEqual(report.iloc[0]['Số lượng SELL-OUT'], 3.0)
        self.assertNotIn('Số lượng SELL-OUT', issues['Trường'].tolist() if not issues.empty else [])

    def test_missing_source_unit_is_reported_as_unit_gap(self):
        report, issues = module.build_report(pd.DataFrame([source(ten_dvt='', ma_dvt='')]), config())
        self.assertTrue(pd.isna(report.iloc[0]['Số lượng SELL-OUT']))
        reasons = issues.loc[issues['Trường'] == 'Số lượng SELL-OUT', 'Lý do'].tolist()
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith(module.UNIT_GAP))
        self.assertIn('thiếu ĐVT', reasons[0])
