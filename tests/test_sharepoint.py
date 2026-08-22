import json
import unittest
from unittest.mock import patch

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


class FakeJsonSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.requests.append((method, url))
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = json.dumps(self.payloads.pop(0)).encode("utf-8")
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

    def test_existing_file_is_replaced_by_item_id(self):
        credential = FakeCredential()
        session = FakeJsonSession(
            [{"id": "item-existing", "size": 3, "webUrl": "https://example/new"}]
        )
        client = SharePointClient(
            "example.sharepoint.com",
            "/sites/Planning",
            "MobiWorkDMS",
            max_retries=0,
            credential=credential,
            session=session,
        )
        client.get_item_by_path = lambda drive_id, remote_path: {
            "id": "item-existing",
            "size": 99,
            "file": {},
        }
        client.ensure_folder_path = lambda drive_id, folder_path: self.fail(
            "existing file overwrite must not create/resolve the parent folder again"
        )

        uploaded = client._put_content(
            "drive-1",
            "01_BaoCaoViengTham/2026/08",
            "BaoCaoViengTham_2026-08-21.xlsx",
            b"abc",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        self.assertEqual(uploaded["size"], 3)
        self.assertEqual(session.requests[0][0], "PUT")
        self.assertTrue(session.requests[0][1].endswith("/items/item-existing/content"))

    def test_upload_rechecks_stale_size_metadata_after_overwrite(self):
        credential = FakeCredential()
        session = FakeJsonSession(
            [
                {"id": "item-1", "size": 99, "webUrl": "https://example/old"},
                {"id": "item-1", "size": 3, "webUrl": "https://example/new"},
            ]
        )
        client = SharePointClient(
            "example.sharepoint.com",
            "/sites/Planning",
            "MobiWorkDMS",
            max_retries=0,
            credential=credential,
            session=session,
        )
        client.get_item_by_path = lambda drive_id, remote_path: {
            "id": "item-1",
            "size": 99,
            "file": {},
        }

        with patch("src.sharepoint.time.sleep") as sleep_mock:
            uploaded = client._put_content(
                "drive-1",
                "01_BaoCaoViengTham/2026/08",
                "BaoCaoViengTham_2026-08-21.xlsx",
                b"abc",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        self.assertEqual(uploaded["size"], 3)
        self.assertEqual(uploaded["webUrl"], "https://example/new")
        self.assertEqual([method for method, _ in session.requests], ["PUT", "GET"])
        sleep_mock.assert_not_called()

    def test_upload_accepts_exact_download_when_metadata_remains_stale(self):
        credential = FakeCredential()
        client = SharePointClient(
            "example.sharepoint.com",
            "/sites/Planning",
            "MobiWorkDMS",
            max_retries=0,
            credential=credential,
            session=FakeSession([]),
        )
        responses = []
        for size in (99, 98):
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({"id": "item-1", "size": size}).encode("utf-8")
            response.headers = {"Content-Type": "application/json"}
            responses.append(response)
        content_response = requests.Response()
        content_response.status_code = 200
        content_response._content = b"abc"
        responses.append(content_response)

        def fake_request(method, url, **kwargs):
            return responses.pop(0)

        client._request = fake_request

        with patch("src.sharepoint.time.sleep"):
            uploaded = client._verify_uploaded_size(
                "drive-1",
                "BaoCaoViengTham_2026-08-21.xlsx",
                {"id": "item-1", "size": 100},
                expected_size=3,
                verification_attempts=2,
                expected_content=b"abc",
            )

        self.assertEqual(uploaded["size"], 3)

    def test_upload_still_fails_when_rechecked_size_never_matches(self):
        credential = FakeCredential()
        session = FakeJsonSession(
            [
                {"id": "item-1", "size": 99},
                {"id": "item-1", "size": 98},
            ]
        )
        client = SharePointClient(
            "example.sharepoint.com",
            "/sites/Planning",
            "MobiWorkDMS",
            max_retries=0,
            credential=credential,
            session=session,
        )

        with (
            patch("src.sharepoint.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "after metadata recheck"),
        ):
            client._verify_uploaded_size(
                "drive-1",
                "BaoCaoViengTham_2026-08-21.xlsx",
                {"id": "item-1", "size": 100},
                expected_size=3,
                verification_attempts=2,
            )


if __name__ == "__main__":
    unittest.main()
