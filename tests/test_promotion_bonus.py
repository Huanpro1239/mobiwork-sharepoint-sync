from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from promotion_bonus import (
    PromotionBonusConfig,
    build_frames,
    fetch_programs,
    fetch_snapshot,
    load_config,
    write_workbook,
)


class FakeMobiWork:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get_json(self, url, params=None, *, operation_key="api", request_number=1):
        self.calls.append(
            {
                "url": url,
                "params": dict(params or {}),
                "operation_key": operation_key,
                "request_number": request_number,
            }
        )
        if not self.payloads:
            raise AssertionError("Unexpected API request")
        return self.payloads.pop(0)


def config() -> PromotionBonusConfig:
    return PromotionBonusConfig(
        enabled=True,
        name="BaoCaoTraThuong",
        folder="06_BaoCaoTraThuong",
        filename="BaoCaoTraThuong_Current.xlsx",
        catalog_url="https://example.test/PromotionBonus",
        report_url="https://example.test/PromotionBonusReport",
        catalog_page_size=2,
    )


class PromotionBonusTests(unittest.TestCase):
    def test_repository_config_matches_openapi_contract(self):
        cfg = load_config()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.folder, "06_BaoCaoTraThuong")
        self.assertEqual(cfg.filename, "BaoCaoTraThuong_Current.xlsx")
        self.assertTrue(cfg.catalog_url.endswith("/OpenAPI/V1/PromotionBonus"))
        self.assertTrue(cfg.report_url.endswith("/OpenAPI/V1/PromotionBonusReport"))
        self.assertLessEqual(cfg.catalog_page_size, 200)

    def test_catalog_paginates_to_api_total(self):
        client = FakeMobiWork(
            [
                {
                    "status": True,
                    "total": 3,
                    "data": [
                        {"_id": "p1", "name": "CT 1"},
                        {"_id": "p2", "name": "CT 2"},
                    ],
                },
                {
                    "status": True,
                    "total": 3,
                    "data": [{"_id": "p3", "name": "CT 3"}],
                },
            ]
        )

        programs = fetch_programs(client, config())

        self.assertEqual([item["_id"] for item in programs], ["p1", "p2", "p3"])
        self.assertEqual([call["params"]["page_number"] for call in client.calls], [1, 2])
        self.assertTrue(all(call["params"]["page_size"] == 2 for call in client.calls))

    def test_report_uses_one_id_per_request_and_preserves_program_provenance(self):
        programs = [
            {"_id": "p1", "name": "CT 1"},
            {"_id": "p2", "name": "CT 2"},
        ]
        client = FakeMobiWork(
            [
                {
                    "status": True,
                    "total": 1,
                    "data": [{"customer": "A", "amount": 100}],
                    "arrChiTieu": [{"metric": "SL"}],
                    "arrTraThuong": [{"level": 1, "bonus": 10}],
                },
                {
                    "status": True,
                    "total": 1,
                    "data": [{"customer": "B", "amount": 200}],
                    "arrChiTieu": [{"metric": "DT"}],
                    "arrTraThuong": [{"level": 2, "bonus": 20}],
                },
            ]
        )

        snapshot = fetch_snapshot(client, config(), programs)

        self.assertEqual(
            [call["params"]["id_ct"] for call in client.calls],
            ["p1", "p2"],
        )
        self.assertEqual(
            [row["promotion_program_id"] for row in snapshot["data"]],
            ["p1", "p2"],
        )
        self.assertEqual(snapshot["targets"][0]["promotion_program_name"], "CT 1")
        self.assertEqual(snapshot["rewards"][1]["promotion_program_name"], "CT 2")

    def test_report_total_mismatch_is_rejected(self):
        client = FakeMobiWork(
            [
                {
                    "status": True,
                    "total": 2,
                    "data": [{"customer": "A"}],
                    "arrChiTieu": [],
                    "arrTraThuong": [],
                }
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "API total=2, fetched=1"):
            fetch_snapshot(client, config(), [{"_id": "p1", "name": "CT 1"}])

    def test_workbook_contains_four_snapshot_sheets(self):
        snapshot = {
            "programs": [{"_id": "p1", "name": "CT 1", "ptype": {"label": "Mua bán"}}],
            "data": [
                {
                    "promotion_program_id": "p1",
                    "promotion_program_name": "CT 1",
                    "customer": "A",
                    "nested": [{"x": 1}],
                }
            ],
            "targets": [],
            "rewards": [{"promotion_program_id": "p1", "bonus": 10}],
        }
        frames = build_frames(snapshot)
        self.assertEqual(
            set(frames),
            {"ChuongTrinh", "Data", "ChiTieu", "TraThuong"},
        )
        self.assertEqual(frames["ChuongTrinh"].iloc[0]["ptype_label"], "Mua bán")

        with tempfile.TemporaryDirectory() as tmp:
            path = write_workbook(
                frames,
                "BaoCaoTraThuong_Current.xlsx",
                Path(tmp),
            )
            with pd.ExcelFile(path, engine="openpyxl") as workbook:
                sheets = workbook.sheet_names
            self.assertEqual(
                sheets,
                ["ChuongTrinh", "Data", "ChiTieu", "TraThuong"],
            )



class PromotionBonusSafetyTests(unittest.TestCase):
    def test_config_validation(self):
        from dataclasses import replace
        for values in [dict(enabled='false'), dict(catalog_page_size=True),
                       dict(catalog_page_size=0), dict(catalog_page_size=201),
                       dict(folder=''), dict(folder='../bad'), dict(filename='../bad.xlsx'),
                       dict(filename='bad.csv'), dict(report_url='')]:
            with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                replace(config(), **values)

    def test_config_requires_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text('[]', encoding='utf-8')
            with self.assertRaises(TypeError):
                load_config(path)

    def test_catalogue_invalid_responses(self):
        cases = [
            ([{'total': 2, 'data': [{'_id': 'a'}]}, {'total': 2, 'data': []}], RuntimeError),
            ([{'data': [{'_id': 'a'}]}, {'data': [{'_id': 'a'}]}], RuntimeError),
            ([{'total': 1, 'data': [{}]}], ValueError),
            ([{'total': 1, 'data': [{'_id': None}]}], ValueError),
            ([{'total': 2, 'data': [{'_id': 'a'}, {'_id': 'a', 'name': 'other'}]}], ValueError),
            ([{'total': 2, 'data': [{'_id': 'a'}, {'_id': 'a'}]}], RuntimeError),
            ([{'total': 2, 'data': [{'_id': 'a'}]}, {'total': 3, 'data': [{'_id': 'b'}]}], RuntimeError),
            ([{'total': 1.5, 'data': []}], TypeError),
            ([{'total': True, 'data': []}], TypeError),
            ([{'total': 'oops', 'data': []}], TypeError),
            ([{'total': -1, 'data': []}], ValueError),
            ([{}], ValueError), ([{'data': {}}], TypeError),
            ([{'data': [None]}], TypeError),
        ]
        for payloads, error in cases:
            with self.subTest(payloads=payloads), self.assertRaises(error):
                fetch_programs(FakeMobiWork(payloads), config())

    def test_no_total_short_pages_and_identical_duplicates(self):
        client = FakeMobiWork([{'data': [{'_id': 'a'}]},
                              {'data': [{'_id': 'a'}, {'_id': 'b'}]}, {'data': []}])
        self.assertEqual(len(fetch_programs(client, config())), 2)
        self.assertEqual(len(client.calls), 3)

    def test_empty_catalogue(self):
        self.assertEqual(fetch_programs(FakeMobiWork([{'total': 0, 'data': []}]), config()), [])
        self.assertEqual(fetch_snapshot(FakeMobiWork([]), config(), [])['data'], [])

    def test_optional_arrays_and_authoritative_provenance(self):
        client = FakeMobiWork([{'total': '1', 'data': [{'promotion_program_id': 'wrong',
                               'promotion_program_name': 'wrong'}]}, {'total': 0, 'data': []}])
        result = fetch_snapshot(client, config(), [{'_id': 'a', 'name': 'Tiếng Việt', 'inactive': True},
                                                   {'_id': 'b', 'archive': True}])
        self.assertEqual(result['data'][0]['promotion_program_id'], 'a')
        self.assertEqual(result['data'][0]['promotion_program_name'], 'Tiếng Việt')
        self.assertEqual(result['targets'], [])
        self.assertEqual(result['rewards'], [])
        self.assertEqual(len(client.calls), 2)

    def test_many_programs_keep_provenance(self):
        programs = [{"_id": str(i), "name": f"Chương trình {i}"} for i in range(250)]
        catalogue = FakeMobiWork([{"total": 250, "data": programs[:200]},
                                 {"total": 250, "data": programs[200:]}])
        from dataclasses import replace
        self.assertEqual(fetch_programs(catalogue, replace(config(), catalog_page_size=200)), programs)
        reports = FakeMobiWork([{"total": 1, "data": [{"value": i}]} for i in range(250)])
        snapshot = fetch_snapshot(reports, config(), programs)
        self.assertEqual(len(reports.calls), 250)
        self.assertEqual([row["promotion_program_id"] for row in snapshot["data"]],
                         [str(i) for i in range(250)])

    def test_status_false_stops_catalogue_and_report(self):
        from unittest.mock import Mock, patch
        from promotion_bonus import MobiWorkClient
        for operation in ("catalogue", "report"):
            with self.subTest(operation=operation):
                client = MobiWorkClient("test-user", "test-token", min_interval_seconds=0)
                response = Mock()
                response.json.return_value = {"status": False, "message": "source failure", "data": []}
                with patch.object(client, "_get_with_retry", return_value=response), \
                     self.assertRaisesRegex(RuntimeError, "status=false"):
                    if operation == "catalogue":
                        fetch_programs(client, config())
                    else:
                        fetch_snapshot(client, config(), [{"_id": "a"}])

    def test_documented_catalogue_products_survive_workbook_roundtrip(self):
        import json
        program = {
            "_id": "6246ca92580d804c253bc22d", "name": "CT trả thưởng tháng 6",
            "ptype": {"value": "1", "label": "Mua bán"},
            "startDate": 1717200000000, "endDate": 1719791999999,
            "isArchived": True,
            "products": [{"_id": "product-1", "ma_san_pham": "SP001",
                          "ten_san_pham": "Sản phẩm A", "khuyen_mai": []}],
        }
        client = FakeMobiWork([{"status": True, "total": 1, "data": [program]},
                              {"status": True, "total": 0, "data": [],
                               "arrChiTieu": [], "arrTraThuong": []}])
        programs = fetch_programs(client, config())
        frames = build_frames(fetch_snapshot(client, config(), programs))
        with tempfile.TemporaryDirectory() as tmp:
            path = write_workbook(frames, "test.xlsx", Path(tmp))
            catalogue = pd.read_excel(path, sheet_name="ChuongTrinh", engine="openpyxl")
            self.assertEqual(json.loads(catalogue.iloc[0]["products"]), program["products"])
            self.assertEqual(catalogue.iloc[0]["ptype_label"], "Mua bán")
            self.assertEqual(catalogue.iloc[0]["startDate"], program["startDate"])
        self.assertEqual(set(client.calls[0]["params"]), {"page_size", "page_number"})
        self.assertEqual(client.calls[1]["params"], {"id_ct": program["_id"]})

    def test_invalid_report_program(self):
        with self.assertRaises(ValueError):
            fetch_snapshot(FakeMobiWork([]), config(), [{'_id': None}])

    def test_nested_serialization_and_excel_literal(self):
        from openpyxl import load_workbook
        snapshot = dict(programs=[{'_id': 'a', 'empty': {}, 'nested': {'label': 'Việt Nam'}}],
                        data=[{'array': [{'x': 'Tiếng Việt'}], 'literal': '=1+1'}], targets=[], rewards=[])
        frames = build_frames(snapshot)
        self.assertEqual(frames['ChuongTrinh'].iloc[0]['empty'], '{}')
        with tempfile.TemporaryDirectory() as tmp:
            path = write_workbook(frames, 'test.xlsx', Path(tmp))
            book = load_workbook(path)
            self.assertEqual(book['Data']['B2'].value, '=1+1')
            self.assertEqual(book['Data']['B2'].data_type, 's')
            self.assertIn('Tiếng Việt', book['Data']['A2'].value)
            book.close()

    def test_flatten_collision_and_cell_limits_rejected(self):
        from promotion_bonus import _frame
        for rows in [[{'a_b': 1, 'a': {'b': 2}}], [{'text': 'x' * 32768}],
                     [{'x' * 32768: 1}], [{str(i): i for i in range(16385)}]]:
            with self.subTest(), self.assertRaises(ValueError):
                _frame(rows, 'Data')

    def test_failed_build_preserves_previous_workbook(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.xlsx'
            path.write_bytes(b'previous workbook')
            with patch('promotion_bonus._format_sheet', side_effect=RuntimeError('format failed')), \
                 self.assertRaises(RuntimeError):
                write_workbook(build_frames(dict(programs=[], data=[], targets=[], rewards=[])),
                               'test.xlsx', Path(tmp))
            self.assertEqual(path.read_bytes(), b'previous workbook')
            self.assertEqual(list(Path(tmp).iterdir()), [path])


class PromotionBonusRunTests(unittest.TestCase):
    def execute(self, *, dry=False, failure=None, noop=False, disabled=False, drive=True):
        import json
        import os
        from dataclasses import replace
        from unittest.mock import Mock, patch
        import promotion_bonus as module
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / 'promotion_bonus_manifest.json'
            client = FakeMobiWork([{'total': 1, 'data': [{'_id': 'a', 'name': 'Việt'}]},
                                   {'total': 1, 'data': [{'amount': 1}]}])
            sharepoint = Mock()
            sharepoint.get_site_id.return_value = 'site'
            sharepoint.get_drive_id.return_value = 'drive'
            sharepoint.upload_file.return_value = dict(upload_skipped=noop, semantic_match=True,
                                                       verification_mode='xlsx_semantic_noop' if noop else 'byte_exact')
            if failure == 'upload':
                sharepoint.upload_file.side_effect = RuntimeError('upload failed')
            if failure == 'state':
                sharepoint.upload_json.side_effect = RuntimeError('state failed')
            actual_write = module.write_workbook
            with patch.object(module, 'MANIFEST_PATH', manifest_path), \
                 patch.object(module, 'load_config', return_value=replace(config(), enabled=not disabled)) as loader, \
                 patch.object(module.MobiWorkClient, 'from_env', return_value=client), \
                 patch.object(module.SemanticSharePointClient, 'from_env', return_value=sharepoint) as factory, \
                 patch.object(module, 'write_workbook', side_effect=lambda frames, filename: actual_write(frames, filename, Path(tmp))), \
                 patch.dict(os.environ, {'DRY_RUN': 'true' if dry else 'false',
                                         'SHAREPOINT_DRIVE_ID': 'drive' if drive else ''}):
                if failure == 'config':
                    loader.side_effect = ValueError('invalid config')
                if failure == 'fetch':
                    client.payloads[1]['total'] = 2
                if failure:
                    with self.assertRaises((RuntimeError, ValueError)):
                        module.run()
                else:
                    result = module.run()
                    self.assertEqual(result['status'], 'skipped' if disabled else 'success')
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                self.assertEqual(manifest['status'], 'failed' if failure else ('skipped' if disabled else 'success'))
                if dry or disabled or failure in {'fetch', 'config'}:
                    factory.assert_not_called()
                    sharepoint.upload_file.assert_not_called()
                    sharepoint.upload_json.assert_not_called()
                elif failure == 'upload':
                    sharepoint.upload_json.assert_not_called()
                else:
                    sharepoint.upload_json.assert_called_once()
                    args = sharepoint.upload_json.call_args.args
                    self.assertEqual(args[1], '06_BaoCaoTraThuong/_sync_state/promotion_bonus.json')
                    self.assertEqual(manifest['sharepoint_write_avoided'], noop)
                    self.assertEqual(args[2]['phase'], 'complete')
                return manifest

    def test_dry_run_builds_workbook_without_sharepoint(self):
        result = self.execute(dry=True)
        self.assertGreater(result['workbook_bytes'], 0)
        self.assertEqual(result['program_count'], 1)

    def test_upload_success_and_current_state(self):
        self.execute(drive=False)

    def test_semantic_noop(self):
        self.execute(noop=True)

    def test_upload_failure_manifest(self):
        self.execute(failure='upload')

    def test_state_failure_manifest(self):
        self.execute(failure='state')

    def test_partial_fetch_never_uploads(self):
        self.execute(failure='fetch')

    def test_config_failure_manifest(self):
        self.execute(failure='config')

    def test_disabled_manifest(self):
        self.execute(disabled=True)


if __name__ == "__main__":
    unittest.main()
