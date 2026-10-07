from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import re

import promotion_api_audit as audit
import promotion_bonus as bonus


class EvidenceTests(unittest.TestCase):
    def test_nested_order_envelopes_are_not_counted_as_customer_reward_rows(self):
        row = {'_id': 'private-id', 'data': {'sdt': {'viewData': 'SECRET'}}}
        envelope = {'RecordType': '_Orders', 'result': [row], 'options': {'$query': {
            'settings.makh': {'$in': ['PRIVATE']},
            'data.ngay_giao_hang.viewData': {'$gte': 1790787600000, '$lte': 1793465999999}}}}
        result = audit.ui_response_summary({'result': [envelope, envelope],
                                           'arrChiTieu': [], 'arrTraThuong': []})
        self.assertEqual(result['nested_record_count'], 2)
        self.assertIsNone(result['customer_rows'])
        self.assertTrue(result['all_envelopes_are_orders'])
        self.assertEqual(result['delivery_date_filters'][0]['to_epoch_ms'], 1793465999999)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('SECRET', json.dumps(result))
        for payload in ({}, {'result': [{'ma': 'customer'}]}, {'result': [{'result': [1]}]}):
            with self.assertRaises(ValueError):
                audit.ui_response_summary(payload)

    def test_ui_contract_matches_captured_post_dates_and_array_without_guessed_calculation(self):
        ids = [f'{i:024x}' for i in range(1, 58)]
        request = audit.ui_report_request('a' * 24, ids, '2026-10-01', '2026-10-31')
        self.assertEqual(request['method'], 'POST')
        self.assertEqual(request['params']['startDate'], 1790787600000)
        self.assertEqual(request['params']['endDate'], 1793379600000)
        self.assertEqual(request['json'], {'arrCT': ids})
        self.assertEqual(len(request['json']['arrCT']), 57)
        self.assertNotIn('sttt', request['params'])
        self.assertNotIn('Authorization', request)
        probe = audit.ui_report_request('a' * 24, ids, '2026-10-01', '2026-10-31', 'observed')
        self.assertEqual(probe['params']['sttt'], 'observed')
        with self.assertRaises(ValueError):
            audit.ui_report_request('a' * 24, ids, '2026-10-01', '2026-10-31', 1)
        for programs in ([], ['invalid'], ['b' * 24, 'b' * 24]):
            with self.assertRaises(ValueError):
                audit.ui_report_request('a' * 24, programs, '2026-10-01', '2026-10-31')

    def setUp(self):
        self.cfg = bonus.load_config()

    def test_current_month_vietnam_dates_and_overrides(self):
        self.assertEqual(audit.date_range(today=date(2026, 10, 6)),
                         (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertEqual(audit.date_range(today=date(2024, 2, 29))[1], date(2024, 2, 29))
        self.assertEqual(audit.date_range('2026-10-01', '2026-10-31')[0], date(2026, 10, 1))
        for start, end in [('bad', ''), ('2026-11-01', '2026-10-31')]:
            with self.assertRaises(ValueError):
                audit.date_range(start, end)

    def test_only_documented_report_parameters(self):
        self.assertEqual(audit.request_parameters('p1'), {'id_ct': 'p1'})
        self.assertEqual(audit.request_parameters('p1', 'explicit-value'),
                         {'id_ct': 'p1', 'sttt': 'explicit-value'})
        for value in ['', 'p1,p2', 'p 1']:
            with self.assertRaises(ValueError):
                audit.request_parameters(value)

    def test_dates_filter_catalogue_not_report_and_calculation_is_unverified(self):
        client = Mock()
        client.get_json.return_value = {'status': True, 'total': 1,
                                       'data': [{'phone': 'SECRET', 'customer': {'region': 'SECRET'}}]}
        with patch.object(audit, 'fetch_programs', return_value=[{'_id': 'p1'}]) as fetch:
            result = audit.run_audit(client, self.cfg, '2026-10-01', '2026-10-31')
        self.assertEqual(fetch.call_args.kwargs['filters'],
                         {'fromdate': '01/10/2026', 'todate': '31/10/2026'})
        self.assertEqual(client.get_json.call_args.args[1], {'id_ct': 'p1'})
        self.assertFalse(result['report_date_binding_verified'])
        self.assertFalse(result['calculation_binding_verified'])
        self.assertEqual(result['customer_rows'], 1)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertIn('customer.region', result['reports'][0]['data']['fields'])

    def test_select_program_and_reject_unknown_or_magic_calculation(self):
        client = Mock()
        client.get_json.return_value = {'status': True, 'total': 0, 'data': []}
        with patch.object(audit, 'fetch_programs', return_value=[{'_id': 'a'}, {'_id': 'b'}]):
            result = audit.run_audit(client, self.cfg, program='b', sttt='observed')
            self.assertEqual(result['program_count'], 1)
            self.assertEqual(client.get_json.call_args.args[1], {'id_ct': 'b', 'sttt': 'observed'})
            with self.assertRaises(ValueError):
                audit.run_audit(client, self.cfg, program='not-found')
        with self.assertRaises(ValueError):
            audit.run_audit(client, self.cfg, calculation_mode='1')

    def test_empty_catalogue_empty_response_and_no_customer_deduplication(self):
        client = Mock()
        with patch.object(audit, 'fetch_programs', return_value=[]):
            self.assertEqual(audit.run_audit(client, self.cfg)['customer_rows'], 0)
        client.get_json.return_value = {'status': True, 'total': 2,
                                       'data': [{'ID': 'same'}, {'ID': 'same', 'level': 2}]}
        with patch.object(audit, 'fetch_programs', return_value=[{'_id': 'a'}, {'_id': 'b'}]):
            self.assertEqual(audit.run_audit(client, self.cfg)['customer_rows'], 4)
        client.get_json.return_value = {'status': True, 'total': 0, 'data': []}
        with patch.object(audit, 'fetch_programs', return_value=[{'_id': 'a'}]):
            self.assertEqual(audit.run_audit(client, self.cfg)['customer_rows'], 0)

    def test_report_date_variants_match_ui_epoch_and_catalogue_format(self):
        first, last = date(2026, 10, 1), date(2026, 10, 31)
        self.assertEqual(audit.report_date_params('id_only', first, last), {})
        self.assertEqual(audit.report_date_params('epoch_ui', first, last),
                         {'startDate': 1790787600000, 'endDate': 1793379600000})
        self.assertEqual(audit.report_date_params('epoch_full_day', first, last),
                         {'startDate': 1790787600000, 'endDate': 1793465999999})
        self.assertEqual(audit.report_date_params('ddmmyyyy', first, last),
                         {'fromdate': '01/10/2026', 'todate': '31/10/2026'})
        with self.assertRaises(ValueError):
            audit.report_date_params('guess', first, last)
        self.assertEqual(audit.parse_variants('epoch_ui, id_only,epoch_ui'), ('epoch_ui', 'id_only'))
        for raw in ('', 'epoch_ui,unknown'):
            with self.assertRaises(ValueError):
                audit.parse_variants(raw)

    def test_each_program_is_probed_per_variant_and_best_variant_reported(self):
        def fake(url, params, **_):
            rows = [{'ma': 'SECRET'}] * 3 if 'startDate' in params else []
            return {'status': True, 'total': len(rows), 'data': rows}
        client = Mock()
        client.get_json.side_effect = fake
        with patch.object(audit, 'fetch_programs', return_value=[{'_id': 'a'}, {'_id': 'b'}]):
            result = audit.run_audit(client, self.cfg, '2026-10-01', '2026-10-31',
                                     date_variants='id_only,epoch_ui,ddmmyyyy')
        self.assertEqual(client.get_json.call_count, 6)
        self.assertEqual(client.get_json.call_args_list[1].args[1],
                         {'id_ct': 'a', 'startDate': 1790787600000, 'endDate': 1793379600000})
        self.assertEqual(result['customer_rows_by_variant'],
                         {'id_only': 0, 'epoch_ui': 6, 'ddmmyyyy': 0})
        self.assertEqual(result['programs_with_rows_by_variant']['epoch_ui'], 2)
        self.assertEqual(result['best_variant'], 'epoch_ui')
        self.assertEqual(result['customer_rows'], 6)
        self.assertFalse(result['report_date_binding_verified'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_no_variant_with_rows_has_no_best_variant(self):
        client = Mock()
        client.get_json.return_value = {'status': True, 'total': 0, 'data': []}
        with patch.object(audit, 'fetch_programs', return_value=[{'_id': 'a'}]):
            result = audit.run_audit(client, self.cfg, date_variants=audit.DATE_VARIANTS)
        self.assertIsNone(result['best_variant'])
        self.assertEqual(result['variants_with_rows'], [])

    def test_schema_does_not_expose_dynamic_contact_or_id_keys(self):
        result = audit.field_paths({'abc@example.com': 1, '6abcad9735cf20e848ef7842': {},
                                    'data': [{'customer_name': 'SECRET'}]})
        self.assertEqual(result, {'data', 'data.customer_name'})

    def test_golden_preserves_subdivision_and_counts_multiple_program_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            frames = {'Region': pd.DataFrame([
                [1, 'C1', 'Miền Bắc > Hà Nội'], [2, 'C1', 'Miền Bắc > Hà Nội'],
                [3, 'C2', 'Miền Bắc'], [4, 'C3', 'Miền Nam']],
                columns=['STT', 'Mã khách hàng', 'Khu vực'])}
            path = bonus.write_workbook(frames, 'golden.xlsx', Path(tmp))
            result = audit.golden_summary(path)
            self.assertEqual(result['customer_rows'], 4)
            self.assertEqual(result['region_distribution'],
                             {'Miền Bắc > Hà Nội': 2, 'Miền Bắc': 1, 'Miền Nam': 1})
            self.assertNotIn('C1', json.dumps(result))

    def test_unverified_only_mode_never_fetches_or_writes(self):
        with patch.dict('os.environ', {'PROMOTION_BONUS_REQUIRE_DMS_MATCH': 'true',
                                       'PROMOTION_BONUS_SOURCE': 'openapi'}), \
             patch.object(bonus, 'MobiWorkClient') as api, \
             patch.object(bonus, 'SemanticSharePointClient') as sp, \
             patch.object(bonus, '_write_manifest'), \
             self.assertRaisesRegex(RuntimeError, 'date parameters'):
            bonus.run()
        api.from_env.assert_not_called()
        sp.from_env.assert_not_called()

    def test_workflow_only_mode_isolated_and_workbooks_never_artifacts(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/mobiwork-sync.yml'
        text = path.read_text(encoding='utf-8')
        steps = []
        for section in text.split('      - name: ')[1:]:
            name = section.splitlines()[0]
            condition = re.search(r'^        if: (.+)$', section, re.MULTILINE)
            steps.append({'name': name, 'if': condition.group(1) if condition else '',
                          'section': section})
        expected = {'Sync Promotion Bonus current snapshot', 'Validate Microsoft OIDC configuration',
                    'Azure login with GitHub OIDC', 'Resolve SharePoint document library'}
        excluded = {'Sync all MobiWork reports', 'Build Data cham anh workbook',
                    'Build promotion detail report', 'Audit PromotionBonusReport API'}
        for step in steps:
            if step['name'] not in expected | excluded:
                continue
            expr = step['if'].strip()[3:-2].strip()
            for old, new in [('github.event_name', 'event'), ('inputs.report_scope', 'scope'),
                             ('inputs.dry_run', 'dry'), ('false', 'False'),
                             ('true', 'True'), ('||', 'or'), ('&&', 'and')]:
                expr = expr.replace(old, new)
            enabled = eval(expr, {'__builtins__': {}},
                           {'event': 'workflow_dispatch', 'scope': 'promotion_bonus_only', 'dry': False})
            self.assertEqual(bool(enabled), step['name'] in expected, step['name'])
        for step in steps:
            if 'uses: actions/upload-artifact' in step['section']:
                artifact_path = step['section'].split('          path:')[1].split('          if-no-files-found:')[0]
                self.assertNotIn('xlsx', artifact_path)


if __name__ == '__main__':
    unittest.main()
