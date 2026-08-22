import io
import unittest
import zipfile
from unittest.mock import Mock

from openpyxl import Workbook

from src.sharepoint_semantic import (
    SemanticSharePointClient,
    workbooks_semantically_equal,
)


class FakeToken:
    token = "token"
    expires_on = 4_000_000_000


class FakeCredential:
    def get_token(self, scope):
        return FakeToken()


def workbook_bytes(value="A"):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Data"
    worksheet.append(["ma", "ten", "so_luong"])
    worksheet.append(["00008", value, 12.5])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def package_with_extra_metadata(content):
    source = io.BytesIO(content)
    target = io.BytesIO()
    with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(
        target, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for item in zin.infolist():
            zout.writestr(item, zin.read(item.filename))
        zout.writestr("sharepoint-package-metadata.txt", "server-side metadata")
    return target.getvalue()


class WorkbookSemanticVerificationTests(unittest.TestCase):
    def test_package_bytes_can_change_without_changing_business_cells(self):
        local = workbook_bytes("Nước khoáng")
        remote = package_with_extra_metadata(local)

        self.assertNotEqual(local, remote)
        matched, details = workbooks_semantically_equal(local, remote)

        self.assertTrue(matched)
        self.assertEqual(
            details["expected_semantic_sha256"],
            details["actual_semantic_sha256"],
        )
        self.assertEqual(details["expected_sheets"], details["actual_sheets"])

    def test_changed_cell_is_rejected(self):
        local = workbook_bytes("Khách hàng A")
        remote = workbook_bytes("Khách hàng B")

        matched, details = workbooks_semantically_equal(local, remote)

        self.assertFalse(matched)
        self.assertNotEqual(
            details["expected_semantic_sha256"],
            details["actual_semantic_sha256"],
        )

    def test_client_accepts_sharepoint_repacked_xlsx(self):
        local = workbook_bytes("Viếng thăm")
        remote = package_with_extra_metadata(local)
        client = SemanticSharePointClient(
            "example.sharepoint.com",
            "/sites/Planning",
            "MobiWorkDMS",
            max_retries=0,
            credential=FakeCredential(),
            session=Mock(),
        )
        client._download_item_content = Mock(return_value=remote)
        response = Mock()
        response.json.return_value = {
            "id": "item-1",
            "name": "Report.xlsx",
            "size": len(remote),
            "webUrl": "https://example/Report.xlsx",
        }
        client._request = Mock(return_value=response)

        verified = client._verify_uploaded_size(
            "drive-1",
            "Report.xlsx",
            {"id": "item-1", "name": "Report.xlsx", "size": len(remote)},
            expected_size=len(local),
            expected_content=local,
        )

        self.assertEqual(verified["verification_mode"], "xlsx_semantic")
        self.assertTrue(verified["semantic_match"])
        self.assertEqual(verified["local_size"], len(local))
        self.assertEqual(verified["size"], len(remote))


if __name__ == "__main__":
    unittest.main()
