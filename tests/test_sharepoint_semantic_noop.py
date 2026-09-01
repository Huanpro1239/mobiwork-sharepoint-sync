import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sharepoint import SharePointClient  # noqa: E402
from sharepoint_semantic import SemanticSharePointClient  # noqa: E402


class DummyCredential:
    def get_token(self, *args, **kwargs):
        raise AssertionError("credential should not be used in this unit test")


def workbook_bytes(value: str) -> bytes:
    buffer = BytesIO()
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Data"
    worksheet["A1"] = "header"
    worksheet["A2"] = value
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


class SemanticNoopUploadTests(unittest.TestCase):
    def make_client(self):
        return SemanticSharePointClient(
            "example.sharepoint.com",
            "/sites/Test",
            "Library",
            credential=DummyCredential(),
        )

    def test_unchanged_xlsx_skips_parent_put(self):
        client = self.make_client()
        local_content = workbook_bytes("same")
        remote_content = workbook_bytes("same")
        existing = {
            "id": "item-1",
            "name": "Report.xlsx",
            "size": len(remote_content),
            "webUrl": "https://example/Report.xlsx",
        }

        with (
            patch.object(client, "get_item_by_path", return_value=existing),
            patch.object(client, "_download_item_content", return_value=remote_content),
            patch.object(SharePointClient, "_put_content") as parent_put,
        ):
            result = client._put_content(
                "drive",
                "Reports/2026/09",
                "Report.xlsx",
                local_content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        parent_put.assert_not_called()
        self.assertTrue(result["upload_skipped"])
        self.assertTrue(result["semantic_match"])
        self.assertEqual(result["verification_mode"], "xlsx_semantic_noop")
        self.assertEqual(result["webUrl"], "https://example/Report.xlsx")

    def test_changed_xlsx_uses_staged_parent_put(self):
        client = self.make_client()
        local_content = workbook_bytes("new")
        remote_content = workbook_bytes("old")
        existing = {"id": "item-1", "name": "Report.xlsx"}
        expected = {"id": "replacement", "verification_mode": "xlsx_semantic"}

        with (
            patch.object(client, "get_item_by_path", return_value=existing),
            patch.object(client, "_download_item_content", return_value=remote_content),
            patch.object(SharePointClient, "_put_content", return_value=expected) as parent_put,
        ):
            result = client._put_content(
                "drive",
                "Reports/2026/09",
                "Report.xlsx",
                local_content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        parent_put.assert_called_once()
        self.assertEqual(result, expected)

    def test_non_xlsx_keeps_existing_parent_behavior(self):
        client = self.make_client()
        expected = {"id": "json"}

        with patch.object(
            SharePointClient,
            "_put_content",
            return_value=expected,
        ) as parent_put:
            result = client._put_content(
                "drive",
                "_sync_runs/2026/09",
                "run.json",
                b"{}",
                "application/json",
            )

        parent_put.assert_called_once()
        self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
