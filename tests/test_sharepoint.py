import json
import unittest
from unittest.mock import Mock, patch

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
    def make_client(self):
        return SharePointClient(
            "example.sharepoint.com",
            "/sites/Planning",
            "MobiWorkDMS",
            max_retries=0,
            credential=FakeCredential(),
            session=FakeSession([]),
        )

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

    def test_new_target_is_uploaded_directly(self):
        client = self.make_client()
        client.get_item_by_path = Mock(return_value=None)
        client.ensure_folder_path = Mock(return_value="parent-1")
        client._upload_new_content = Mock(
            return_value={"id": "new-1", "size": 3, "webUrl": "https://example/new"}
        )

        result = client._put_content(
            "drive-1",
            "01_BaoCaoViengTham/2026/08",
            "BaoCaoViengTham_2026-08-21.xlsx",
            b"abc",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        self.assertEqual(result["id"], "new-1")
        client.ensure_folder_path.assert_called_once_with(
            "drive-1", "01_BaoCaoViengTham/2026/08"
        )
        args = client._upload_new_content.call_args.args
        self.assertEqual(args[1:4], ("parent-1", "BaoCaoViengTham_2026-08-21.xlsx", b"abc"))

    def test_existing_target_uses_staged_swap_and_removes_backup(self):
        client = self.make_client()
        existing = {
            "id": "old-1",
            "size": 99,
            "eTag": '"old-etag"',
            "file": {},
            "parentReference": {"id": "parent-1"},
        }
        client._upload_new_content = Mock(
            return_value={"id": "temp-1", "size": 3, "webUrl": "https://example/temp"}
        )
        client._rename_item = Mock(
            side_effect=[
                {"id": "old-1", "name": "backup"},
                {"id": "temp-1", "name": "BaoCaoViengTham_2026-08-21.xlsx"},
            ]
        )
        client._verify_exact_item_content = Mock(
            return_value={"id": "temp-1", "size": 3, "webUrl": "https://example/final"}
        )
        client._delete_item = Mock()

        with patch("src.sharepoint.uuid4") as uuid_mock:
            uuid_mock.return_value.hex = "abcdef1234567890"
            result = client._staged_replace_content(
                "drive-1",
                "01_BaoCaoViengTham/2026/08",
                "BaoCaoViengTham_2026-08-21.xlsx",
                b"abc",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                existing,
            )

        self.assertEqual(result["size"], 3)
        upload_args = client._upload_new_content.call_args.args
        self.assertEqual(upload_args[1], "parent-1")
        self.assertTrue(upload_args[2].startswith("__sync_tmp_abcdef123456__"))
        rename_calls = client._rename_item.call_args_list
        self.assertEqual(rename_calls[0].args[1], "old-1")
        self.assertTrue(rename_calls[0].args[2].startswith("__sync_backup_abcdef123456__"))
        self.assertEqual(rename_calls[1].args[1:3], ("temp-1", "BaoCaoViengTham_2026-08-21.xlsx"))
        client._verify_exact_item_content.assert_called_once_with(
            "drive-1",
            "temp-1",
            "BaoCaoViengTham_2026-08-21.xlsx",
            b"abc",
        )
        client._delete_item.assert_called_once_with("drive-1", "old-1")

    def test_staged_swap_rolls_back_when_promotion_fails(self):
        client = self.make_client()
        existing = {
            "id": "old-1",
            "size": 99,
            "file": {},
            "parentReference": {"id": "parent-1"},
        }
        client._upload_new_content = Mock(return_value={"id": "temp-1", "size": 3})
        client._rename_item = Mock(
            side_effect=[
                {"id": "old-1", "name": "backup"},
                RuntimeError("promotion failed"),
                {"id": "old-1", "name": "BaoCaoViengTham_2026-08-21.xlsx"},
            ]
        )
        client._delete_item = Mock()

        with (
            patch("src.sharepoint.uuid4") as uuid_mock,
            self.assertRaisesRegex(RuntimeError, "promotion failed"),
        ):
            uuid_mock.return_value.hex = "abcdef1234567890"
            client._staged_replace_content(
                "drive-1",
                "01_BaoCaoViengTham/2026/08",
                "BaoCaoViengTham_2026-08-21.xlsx",
                b"abc",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                existing,
            )

        rename_calls = client._rename_item.call_args_list
        self.assertEqual(len(rename_calls), 3)
        self.assertEqual(rename_calls[2].args[1:3], ("old-1", "BaoCaoViengTham_2026-08-21.xlsx"))
        client._delete_item.assert_called_once_with("drive-1", "temp-1")

    def test_upload_rechecks_stale_size_metadata(self):
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

        with patch("src.sharepoint.time.sleep") as sleep_mock:
            uploaded = client._verify_uploaded_size(
                "drive-1",
                "BaoCaoViengTham_2026-08-21.xlsx",
                {"id": "item-1", "size": 100},
                expected_size=3,
                verification_attempts=2,
            )

        self.assertEqual(uploaded["size"], 3)
        self.assertEqual(uploaded["webUrl"], "https://example/new")
        sleep_mock.assert_called_once_with(1.0)

    def test_upload_accepts_exact_download_when_metadata_remains_stale(self):
        client = self.make_client()
        responses = []
        for size in (99, 98):
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({"id": "item-1", "size": size}).encode("utf-8")
            response.headers = {"Content-Type": "application/json"}
            responses.append(response)

        def fake_request(method, url, **kwargs):
            return responses.pop(0)

        client._request = fake_request
        client._download_item_content = Mock(return_value=b"abc")

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
