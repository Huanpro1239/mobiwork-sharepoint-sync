from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import yaml

import promotion_api_audit as audit
import promotion_bonus as bonus


class EvidenceTests(unittest.TestCase):
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
        with patch.dict('os.environ', {'PROMOTION_BONUS_REQUIRE_DMS_MATCH': 'true'}), \
             patch.object(bonus, 'MobiWorkClient') as api, \
             patch.object(bonus, 'SemanticSharePointClient') as sp, \
             patch.object(bonus, '_write_manifest'), \
             self.assertRaisesRegex(RuntimeError, 'date parameters'):
            bonus.run()
        api.from_env.assert_not_called()
        sp.from_env.assert_not_called()

    def test_workflow_only_mode_isolated_and_workbooks_never_artifacts(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/mobiwork-sync.yml'
        workflow = yaml.safe_load(path.read_text(encoding='utf-8'))
        steps = workflow['jobs']['sync']['steps']
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
            if 'upload-artifact' in step.get('uses', ''):
                self.assertNotIn('xlsx', step['with']['path'])


if __name__ == '__main__':
    unittest.main()
