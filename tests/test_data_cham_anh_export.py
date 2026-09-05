import sys
import tempfile
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path

import pandas as pd

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from data_cham_anh_export import (  # noqa: E402
    DATA_ANH_COLUMNS,
    DATA_DON_HANG_COLUMNS,
    build_data_anh_frame,
    build_data_don_hang_frame,
    publish_data_cham_anh_month,
    write_data_cham_anh_workbook,
)
from mobiwork import ReportConfig  # noqa: E402


class FakeSharePoint:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.downloaded_paths: list[str] = []
        self.uploaded_folder: str | None = None
        self.uploaded_name: str | None = None
        self.uploaded_bytes: bytes | None = None

    def download_file_bytes(self, drive_id: str, remote_path: str) -> bytes | None:
        self.downloaded_paths.append(remote_path)
        return self.files.get(remote_path)

    def upload_file(self, drive_id: str, path: Path, remote_folder: str):
        self.uploaded_folder = remote_folder
        self.uploaded_name = path.name
        self.uploaded_bytes = path.read_bytes()
        return {
            "size": path.stat().st_size,
            "verification_mode": "xlsx_semantic",
            "semantic_match": True,
            "upload_skipped": False,
            "webUrl": f"https://example/{remote_folder}/{path.name}",
        }


def workbook_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
    return buffer.getvalue()


class DataChamAnhTransformTests(unittest.TestCase):
    def test_data_anh_uses_vung_not_loai_kh_and_explodes_image_urls(self):
        source = pd.DataFrame(
            [
                {
                    "ten_nhan_vien": "Lương Văn Tuấn",
                    "ngay": pd.Timestamp("2026-09-03"),
                    "ma_kh": "BAGI022464",
                    "ten_kh": "Tạp hóa Hảo Cường",
                    "loai_kh": "Nhà thuốc",
                    "vung": "Miền Bắc",
                    "hinh_anh": (
                        "https://dmsimages.mobiwork.vn/a.jpg; "
                        "https://dmsimages.mobiwork.vn/b.jpg"
                    ),
                    "ghi_ton": True,
                    "ghi_chu": "đủ ảnh",
                }
            ]
        )

        result = build_data_anh_frame(source)

        self.assertEqual(result.columns.tolist(), DATA_ANH_COLUMNS)
        self.assertNotIn("loai_kh", result.columns)
        self.assertEqual(result["vung"].tolist(), ["Miền Bắc", "Miền Bắc"])
        self.assertEqual(result["stt_hinh"].tolist(), [1, 2])
        self.assertEqual(result["so_hinh"].tolist(), [2, 2])
        self.assertEqual(
            result["hinh_anh"].tolist(),
            [
                "https://dmsimages.mobiwork.vn/a.jpg",
                "https://dmsimages.mobiwork.vn/b.jpg",
            ],
        )

    def test_data_don_hang_matches_sample_bill_columns_and_preserves_codes(self):
        source = pd.DataFrame(
            [
                {
                    "ma_kh": "HCMC062030",
                    "ten_kh": "NHÀ THUỐC TÂY KHÔI NGUYÊN",
                    "ngay_dat": pd.Timestamp("2026-08-31 23:50:41"),
                    "ten_nhom": "HCMC",
                    "ma_nv_dat": "HCMC0603",
                    "ten_nguoi_dat": "Lê Quang Huy",
                    "ma_nv_duyet": "HCMC06",
                    "nguoi_duyet": "AD TRUNG DUNG",
                    "ngay_duyet": pd.Timestamp("2026-08-31 23:50:41"),
                    "ngay_tao": pd.Timestamp("2026-08-31 23:50:41"),
                    "ma_nguoi_tao": "HCMC06",
                    "nguoi_tao": "AD TRUNG DUNG",
                    "dien_giai": "Bán hàng theo phiếu đặt hàng số [ĐH_027442026]",
                    "ma_sp": "00008",
                    "ten_sp": "Đảnh thạnh có gas 430ml",
                    "ma_dvt": "Thùng",
                    "ma_kho_xuat": "B-HCMC-1035",
                    "so_luong": 1,
                    "don_gia": 141500,
                    "extra_field": "không xuất",
                }
            ]
        )

        result = build_data_don_hang_frame(source)

        self.assertEqual(result.columns.tolist(), DATA_DON_HANG_COLUMNS)
        self.assertEqual(result.iloc[0]["ma_sp"], "00008")
        self.assertNotIn("extra_field", result.columns)

    def test_writer_creates_exact_two_sheet_workbook(self):
        image_frame = pd.DataFrame(
            [["NV A", pd.Timestamp("2026-09-03"), "KH01", "Tạp hóa A", "Miền Bắc", 1, "https://dmsimages.mobiwork.vn/a.jpg", 1, True, None]],
            columns=DATA_ANH_COLUMNS,
        )
        bill_frame = pd.DataFrame(
            [["KH01", "Tạp hóa A", pd.Timestamp("2026-09-03"), "HCM", "NV01", "NV A", "DUYET", "Admin", pd.Timestamp("2026-09-03"), pd.Timestamp("2026-09-03"), "DUYET", "Admin", "Bán hàng", "00008", "SP A", "Thùng", "KHO01", 1, 100000]],
            columns=DATA_DON_HANG_COLUMNS,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_data_cham_anh_workbook(
                image_frame,
                bill_frame,
                date(2026, 9, 3),
                output_dir=Path(temp_dir),
            )
            workbook = pd.ExcelFile(path, engine="openpyxl")
            self.assertEqual(workbook.sheet_names, ["Data_anh", "Data_don_hang"])
            data_anh = pd.read_excel(path, sheet_name="Data_anh", engine="openpyxl")
            data_don_hang = pd.read_excel(path, sheet_name="Data_don_hang", engine="openpyxl")

        self.assertEqual(data_anh.columns.tolist(), DATA_ANH_COLUMNS)
        self.assertEqual(data_don_hang.columns.tolist(), DATA_DON_HANG_COLUMNS)

    def test_publish_reads_visit_and_bill_monthly_masters_then_uploads_combined_file(self):
        target = date(2026, 9, 3)
        visit_path = "01_BaoCaoViengTham/2026/09/BaoCaoViengTham_2026-09.xlsx"
        bill_path = "04_DonBanHang/2026/09/DonBanHang_2026-09.xlsx"
        visit_master = workbook_bytes(
            {
                "Data": pd.DataFrame(
                    [
                        {
                            "_sync_date": "2026-09-03",
                            "ten_nhan_vien": "NV A",
                            "ngay": pd.Timestamp("2026-09-03"),
                            "ma_kh": "KH01",
                            "ten_kh": "Tạp hóa A",
                            "loai_kh": "Nhà thuốc",
                            "vung": "Miền Bắc",
                            "hinh_anh": "https://dmsimages.mobiwork.vn/a.jpg",
                            "ghi_ton": True,
                        }
                    ]
                )
            }
        )
        bill_detail = pd.DataFrame(
            [
                {
                    "_sync_date": "2026-09-03",
                    "ma_kh": "KH01",
                    "ten_kh": "Tạp hóa A",
                    "ngay_dat": pd.Timestamp("2026-09-03"),
                    "ten_nhom": "HCM",
                    "ma_nv_dat": "NV01",
                    "ten_nguoi_dat": "NV A",
                    "ma_nv_duyet": "DUYET",
                    "nguoi_duyet": "Admin",
                    "ngay_duyet": pd.Timestamp("2026-09-03"),
                    "ngay_tao": pd.Timestamp("2026-09-03"),
                    "ma_nguoi_tao": "DUYET",
                    "nguoi_tao": "Admin",
                    "dien_giai": "Bán hàng",
                    "ma_sp": "00008",
                    "ten_sp": "SP A",
                    "ma_dvt": "Thùng",
                    "ma_kho_xuat": "KHO01",
                    "so_luong": 1,
                    "don_gia": 100000,
                }
            ]
        )
        bill_master = workbook_bytes(
            {"DonHang": pd.DataFrame([{"ma_phieu": "BH01"}]), "ChiTietSP": bill_detail}
        )
        sharepoint = FakeSharePoint({visit_path: visit_master, bill_path: bill_master})
        reports = [
            ReportConfig(
                key="visit",
                enabled=True,
                name="BaoCaoViengTham",
                folder="01_BaoCaoViengTham",
            ),
            ReportConfig(
                key="bill",
                enabled=True,
                name="DonBanHang",
                folder="04_DonBanHang",
            ),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            result = publish_data_cham_anh_month(
                reports,
                sharepoint,
                "drive",
                target,
                root_folder="05_DataChamAnh",
                output_dir=Path(temp_dir),
            )

        self.assertEqual(sharepoint.downloaded_paths, [visit_path, bill_path])
        self.assertEqual(sharepoint.uploaded_folder, "05_DataChamAnh/2026/09")
        self.assertEqual(sharepoint.uploaded_name, "Data_cham_anh_2026-09.xlsx")
        self.assertEqual(result["data_anh_rows"], 1)
        self.assertEqual(result["data_don_hang_rows"], 1)
        self.assertEqual(result["remote_folder"], "05_DataChamAnh/2026/09")
        self.assertIsNotNone(sharepoint.uploaded_bytes)

        uploaded = BytesIO(sharepoint.uploaded_bytes)
        image_sheet = pd.read_excel(uploaded, sheet_name="Data_anh", engine="openpyxl")
        uploaded.seek(0)
        bill_sheet = pd.read_excel(uploaded, sheet_name="Data_don_hang", engine="openpyxl")
        self.assertEqual(image_sheet.columns.tolist(), DATA_ANH_COLUMNS)
        self.assertEqual(bill_sheet.columns.tolist(), DATA_DON_HANG_COLUMNS)
        self.assertEqual(image_sheet.iloc[0]["vung"], "Miền Bắc")
        self.assertNotIn("loai_kh", image_sheet.columns)


if __name__ == "__main__":
    unittest.main()
