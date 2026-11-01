import io
import os
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import Mock

from src.fit_sales_sync import (
    DRIVE, ORIGIN, REPORTS, ROUTE, XLSM_TYPE, SalesExportClient, SalesSyncError, parse_period, publish, run,
    workbook_extension,
)

TODAY = date(2026, 11, 1)


def workbook(macro=True):
    kind = b"application/vnd.ms-excel.sheet.macroEnabled.main+xml" if macro else b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", b'<Types><Override PartName="/xl/workbook.xml" ContentType="' + kind + b'"/></Types>')
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return output.getvalue()


def response(status=200, content=b"", payload=None):
    result = Mock(status_code=status, content=content)
    if payload is None:
        result.json.side_effect = ValueError
    else:
        result.json.return_value = payload
    return result


class PeriodTests(unittest.TestCase):
    def test_same_grammar_as_telegram_command(self):
        self.assertEqual(parse_period("", date(2026, 10, 20)), (date(2026, 10, 1), date(2026, 10, 20)))
        self.assertEqual(parse_period("thangtruoc", TODAY), (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertEqual(parse_period("9/2026", TODAY), (date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(parse_period("2026-10-01 2026-10-15", TODAY), (date(2026, 10, 1), date(2026, 10, 15)))
        self.assertEqual(parse_period("01/10/2026 15/10/2026", TODAY), (date(2026, 10, 1), date(2026, 10, 15)))
        for bad in ("15/10/2026 01/10/2026", "12/2026", "01/12/2026", "a b c", "2024-01-01 2026-01-01"):
            with self.assertRaises(SalesSyncError, msg=bad):
                parse_period(bad, TODAY)


class WorkbookTests(unittest.TestCase):
    def test_extension_follows_content(self):
        self.assertEqual(workbook_extension(workbook(True)), ".xlsm")
        self.assertEqual(workbook_extension(workbook(False)), ".xlsx")
        for bad in (b"", b"<html><form name=loginForm>", b"PK\x03\x04broken"):
            with self.assertRaises(SalesSyncError):
                workbook_extension(bad)


class ClientTests(unittest.TestCase):
    def test_only_the_approved_route_with_access_headers_and_busy_retry(self):
        session = Mock()
        session.get.side_effect = [response(409, payload={"code": "ERP_BUSY"}), response(200, workbook())]
        sleeps = []
        client = SalesExportClient("id.access", "secret", session=session, sleep=sleeps.append)
        self.assertEqual(client.fetch(REPORTS[1], date(2026, 10, 1), date(2026, 10, 31)), workbook())
        args, kwargs = session.get.call_args
        self.assertEqual(args[0], ORIGIN + ROUTE)
        self.assertEqual(kwargs["params"], {"start": "2026-10-01", "end": "2026-10-31", "muc": "hoa_don"})
        self.assertEqual(kwargs["headers"]["CF-Access-Client-Id"], "id.access")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(sleeps, [30])

    def test_clear_errors(self):
        cases = [
            (response(401, payload={"code": "ERP_LOGIN_REQUIRED"}), "/dangnhap"),
            (response(302), "Cloudflare Access"),
            (response(422, payload={"code": "REPORT_NOT_EXPORTED", "error": "Chưa chọn ngày"}), "Chưa chọn ngày"),
            (response(200, b"<html></html>"), "không phải Excel"),
        ]
        for resp, text in cases:
            session = Mock()
            session.get.return_value = resp
            with self.assertRaises(SalesSyncError) as ctx:
                SalesExportClient("id", "s", session=session, sleep=lambda s: None).fetch(REPORTS[0], TODAY, TODAY)
            self.assertIn(text, str(ctx.exception))
        with self.assertRaises(SalesSyncError):
            SalesExportClient("", "")


class PublishTests(unittest.TestCase):
    def test_names_and_macro_content_type(self):
        graph = Mock()
        graph._put_content.return_value = {"verification_mode": "byte_exact"}
        publish(graph, "DH ban trong ky NPP", "Lay So Hoa don.xlsm", b"x")
        graph._put_content.assert_called_once_with(DRIVE, "DH ban trong ky NPP", "Lay So Hoa don.xlsm", b"x", XLSM_TYPE)
        with self.assertRaises(ValueError):
            publish(graph, "x", "../evil.xlsm", b"x")


class FakeClient:
    def __init__(self, error=None):
        self.error, self.seen = error, []

    def fetch(self, report, start, end):
        self.seen.append(report.muc)
        if self.error:
            raise SalesSyncError(self.error)
        return workbook()


class RunTests(unittest.TestCase):
    def test_publishes_both_files_with_user_names_and_notifies(self):
        graph, notifier = Mock(), Mock()
        graph._put_content.return_value = {"webUrl": "https://sp/x", "verification_mode": "byte_exact"}
        rc = run(date(2026, 10, 1), date(2026, 10, 31), client=FakeClient(), graph_factory=lambda: graph, notifier=notifier)
        self.assertEqual(rc, 0)
        names = [c.args[2] for c in graph._put_content.call_args_list]
        self.assertEqual(names, ["BCDonHangBanTrongKyNPP.xlsm", "Lay So Hoa don.xlsm"])
        self.assertEqual([c.args[0] for c in notifier.document.call_args_list], names)
        self.assertTrue(notifier.message.call_args.args[0].startswith("✅"))

    def test_nothing_written_when_one_export_fails(self):
        notifier = Mock()

        def no_graph():
            raise AssertionError("SharePoint must not be touched")

        rc = run(TODAY, TODAY, client=FakeClient("Phiên FIT đã hết hạn"), graph_factory=no_graph, notifier=notifier)
        self.assertEqual(rc, 1)
        self.assertIn("Phiên FIT đã hết hạn", notifier.message.call_args.args[0])

    def test_dry_run_writes_locally(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                self.assertEqual(run(TODAY, TODAY, client=FakeClient(), graph_factory=None, dry_run=True), 0)
                self.assertTrue(Path("BCDonHangBanTrongKyNPP.xlsm").exists())
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
