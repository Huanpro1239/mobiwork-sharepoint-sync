import unittest
from unittest import mock

import requests

from src.sharepoint import SharePointClient


class FakeToken:
    def __init__(self, token, expires_on):
        self.token = token
        self.expires_on = expires_on


class FakeCredential:
    def __init__(self):
        self.calls = 0

    def get_token(self, scope):
        self.calls += 1
        return FakeToken(f"token-{self.calls}", 4_000_000_000)


class FakeSession:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.authorization_headers = []

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.authorization_headers.append((headers or {}).get("Authorization"))
        response = requests.Response()
        response.status_code = self.statuses.pop(0)
        response.url = url
        response._content = b"{}"
        response.headers = {"Content-Type": "application/json"}
        return response


class SharePointClientTests(unittest.TestCase):
    def test_401_forces_token_refresh(self):
        credential = FakeCredential()
        session = FakeSession([401, 200])
        client = SharePointClient(
            "example.sharepoint.com",
            "/sites/Planning",
            "MobiWorkDMS",
            max_retries=1,
            credential=credential,
            session=session,
        )

        response = client._request("GET", "https://graph.microsoft.com/v1.0/test")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(credential.calls, 2)
        self.assertEqual(
            session.authorization_headers,
            ["Bearer token-1", "Bearer token-2"],
        )

    def test_cached_token_is_reused_while_valid(self):
        credential = FakeCredential()
        session = FakeSession([200, 200])
        client = SharePointClient(
            "example.sharepoint.com",
            "/sites/Planning",
            "MobiWorkDMS",
            max_retries=0,
            credential=credential,
            session=session,
        )

        client._request("GET", "https://graph.microsoft.com/v1.0/one")
        client._request("GET", "https://graph.microsoft.com/v1.0/two")

        self.assertEqual(credential.calls, 1)

    def test_binary_upload_size_mismatch_logs_warning_only(self):
        client = SharePointClient(
            "example.sharepoint.com",
            "/sites/Planning",
            "MobiWorkDMS",
            credential=FakeCredential(),
            session=FakeSession([200]),
        )
        payload = {"size": 20}
        with (
            mock.patch.object(client, "ensure_folder_path", return_value="folder-id"),
            mock.patch.object(client, "_request") as request_mock,
            self.assertLogs("mobiwork_sync", level="WARNING") as logs,
        ):
            request_mock.return_value.json.return_value = payload
            uploaded = client._put_content(
                "drive-id",
                "Reports",
                "report.xlsx",
                b"1234567890",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        self.assertEqual(uploaded, payload)
        self.assertIn("SharePoint upload size mismatch for report.xlsx", "\n".join(logs.output))

    def test_json_upload_size_mismatch_raises(self):
        client = SharePointClient(
            "example.sharepoint.com",
            "/sites/Planning",
            "MobiWorkDMS",
            credential=FakeCredential(),
            session=FakeSession([200]),
        )
        with (
            mock.patch.object(client, "ensure_folder_path", return_value="folder-id"),
            mock.patch.object(client, "_request") as request_mock,
        ):
            request_mock.return_value.json.return_value = {"size": 20}
            with self.assertRaises(RuntimeError):
                client._put_content(
                    "drive-id",
                    "_sync_runs",
                    "sync_manifest.json",
                    b"1234567890",
                    "application/json; charset=utf-8",
                )


if __name__ == "__main__":
    unittest.main()
