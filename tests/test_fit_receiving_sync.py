import io
import unittest
import zipfile
from unittest.mock import Mock
from xml.sax.saxutils import escape

import requests

from src.fit_receiving_sync import (
    DRIVE, FOLDER, ORIGIN, ROOT, SHEETS, FITExportClient, publish, validate_workbook,
)

JOB = "12345678-1234-1234-1234-123456789012"


def workbook(sheets=SHEETS):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("xl/workbook.xml", '<workbook><sheets>' + ''.join(f'<sheet name="{escape(name)}"/>' for name in sheets) + '</sheets></workbook>')
    return output.getvalue()


def response(status=200, payload=None, content=None):
    result = Mock(status_code=status)
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.json.return_value = payload
    result.headers = {"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    result.iter_content.return_value = [content or b""]
    return result


class FITReceivingTests(unittest.TestCase):
    def client(self, responses):
        session = Mock()
        session.request.side_effect = responses
        return FITExportClient("test-only.access", "test-only-secret", session=session, sleep=lambda _: None), session

    def job(self, **extra):
        return {"id": JOB, "start": "2026-10-01", "end": "2026-10-31", **extra}

    def test_stable_monthly_name_ready_download_and_no_redirects(self):
        blob = workbook()
        client, session = self.client([
            response(202, self.job()), response(payload=self.job(phase="ready")), response(content=blob),
        ])
        content, filename = client.export("github-run-1")
        self.assertEqual((content, filename), (blob, "Nhan_hang_2026_10.xlsx"))
        for call in session.request.call_args_list:
            self.assertTrue(call.args[1].startswith(ORIGIN + ROOT))
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_login_expired_or_failed_never_downloads_or_publishes(self):
        for phase in ["needs_login", "failed", "expired", "unknown"]:
            client, session = self.client([response(202, self.job()), response(payload=self.job(phase=phase))])
            with self.assertRaises(RuntimeError):
                client.export("github-run-1")
            self.assertEqual(session.request.call_count, 2)

    def test_changed_job_period_and_bad_date_fail_closed(self):
        client, _ = self.client([response(202, self.job()), response(payload=self.job(phase="ready", end="2026-10-30"))])
        with self.assertRaises(ValueError):
            client.export("github-run-1")
        client, _ = self.client([response(202, self.job(start="2026-13-01", end="2026-13-31"))])
        with self.assertRaises(ValueError):
            client.export("github-run-1")

    def test_credentials_cannot_follow_redirects_or_paths_outside_integration(self):
        client, session = self.client([response(302)])
        with self.assertRaises(RuntimeError):
            client.request("POST", ROOT, "test")
        with self.assertRaises(ValueError):
            client.request("GET", "https://another.example/file")
        self.assertEqual(session.request.call_count, 1)

    def test_uncertain_post_is_not_automatically_repeated(self):
        client, session = self.client([requests.Timeout("test-only timeout")])
        with self.assertRaises(requests.Timeout):
            client.export("github-run-1")
        self.assertEqual(session.request.call_count, 1)

    def test_busy_wait_is_bounded(self):
        client, session = self.client([response(409)])
        client.clock = Mock(side_effect=[0, 0, 31])
        with self.assertRaises(TimeoutError):
            client.export("github-run-1", timeout=30)
        self.assertEqual(session.request.call_count, 1)

    def test_incomplete_workbook_and_arbitrary_name_never_upload(self):
        graph = Mock()
        with self.assertRaises(ValueError):
            publish(graph, workbook(SHEETS[:2]), "Nhan_hang_2026_10.xlsx")
        with self.assertRaises(ValueError):
            publish(graph, workbook(), "../../other.xlsx")
        graph.upload_file.assert_not_called()
        with self.assertRaises(zipfile.BadZipFile):
            validate_workbook(b"<html>Login</html>")

    def test_only_approved_folder_is_written_after_full_validation(self):
        graph = Mock()
        graph.get_item_by_path.return_value = {"folder": {}}
        graph.upload_file.return_value = {"semantic_match": True, "verification_mode": "byte_exact"}
        self.assertTrue(publish(graph, workbook(), "Nhan_hang_2026_10.xlsx")["semantic_match"])
        graph.get_item_by_path.assert_called_once_with(DRIVE, FOLDER)
        args = graph.upload_file.call_args.args
        self.assertEqual((args[0], args[1].name, args[2]), (DRIVE, "Nhan_hang_2026_10.xlsx", FOLDER))
        self.assertFalse(args[1].exists())  # Temporary business file was removed.


if __name__ == "__main__":
    unittest.main()
