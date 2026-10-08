import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pandas as pd

import bosung_mapping as bosung
from promotion_detail import build_report, resolve_sales, warehouse


def bill(**overrides):
    row = {"ma_phieu": "DH1", "stt": "1", "ma_kh": "KH1", "ID_khachhang": "ID1", "ten_kh": "KH Một",
           "ma_nv_dat": "NV1", "ten_nguoi_dat": "Nhân viên", "ngay_dat": "2026-07-09",
           "ma_sp": "SP1", "ten_sp": "Sản phẩm", "ten_dvt": "Chai", "ma_dvt": "Chai", "so_luong": 24,
           "gia_truoc_vat": 10, "is_km": False, "loai_hang": "Bán hàng", "ctkm": "001/TB/GT/07/2026_A",
           "ma_kho_xuat": "B-KHHO-0288", "loai_kh": "Tạp hóa", "tuyen_code": "R1"}
    row.update(overrides)
    return row


def base_cfg():
    return {"products": {"SP1": {"Brand": "Vikoda", "Package": "1 Way"}},
            "unit_conversions": {"SP1|Chai": {"target_unit": "Thùng", "factor": "0.04166666666666666666666666667"}},
            "employees": {"NV1": {"DB Code": "B-DANA-0001", "Tên NPP": "NPP hiện tại", "SS Code": "GSX",
                                  "SS Name": "GS hiện tại", "Vùng": "Miền Trung 2"}},
            "employees_explicit": {},
            "npp_units": {"B-KHHO-0288": {"DB Code": "B-KHHO-0288", "Tên NPP": "TU TAI", "Vùng": "Miền Trung 1B"}},
            "supervisor_candidates": {"KHHO": "KHA04 - Huỳnh Thanh Cường"},
            "customer_catalogue": {"ID1": {"Tỉnh": "Khánh Hòa", "customer_code": "KH1"}},
            "program_codes": {}, "employee_regions": {}}


def workbook(sheets):
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, rows in sheets.items():
            pd.DataFrame(rows).to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


class WarehouseResolutionTests(unittest.TestCase):
    def test_order_warehouse_beats_current_employee_unit(self):
        self.assertEqual(warehouse({"ma_kho_xuat_km": "KM - B-QUNA-0044"}), "B-QUNA-0044")
        self.assertEqual(warehouse({"ma_kho_xuat": "KHO TONG"}), "")
        resolved = resolve_sales(bill(), base_cfg())
        self.assertEqual(resolved["DB Code"], "B-KHHO-0288")
        self.assertEqual(resolved["Tên NPP"], "TU TAI")
        self.assertNotIn("SS Code", resolved)  # never the SS of the employee's *current* NPP

    def test_without_warehouse_falls_back_to_employee(self):
        resolved = resolve_sales(bill(ma_kho_xuat=None), base_cfg())
        self.assertEqual(resolved["DB Code"], "B-DANA-0001")

    def test_explicit_and_user_overrides_win(self):
        cfg = base_cfg()
        cfg["employees_explicit"] = {"NV1": {"Vùng": "Vùng tay"}}
        cfg["npp_overrides"] = {"B-KHHO-0288": {"SS Code": "KHA04", "SS Name": "Cường"}}
        cfg["employee_overrides"] = {"NV1": {"Tên NPP": "Tên sửa"}}
        resolved = resolve_sales(bill(), cfg)
        self.assertEqual((resolved["SS Code"], resolved["Vùng"], resolved["Tên NPP"]),
                         ("KHA04", "Vùng tay", "Tên sửa"))


class FillInTests(unittest.TestCase):
    def test_issues_become_one_row_per_key_with_hints(self):
        cfg = base_cfg()
        detail = pd.DataFrame([bill(), bill(stt="2"), bill(stt="3", ma_sp="SP2", ten_dvt="Lốc", ma_dvt="Lốc")],
                              dtype=object)
        report, issues = build_report(detail, cfg)
        rows = bosung.aggregate(bosung.todo_rows(issues, cfg), "CTKM 2026-07")
        frames = bosung.todo_frames({"CTKM 2026-07": rows})
        npp = frames["NPP"].iloc[0]
        self.assertEqual(npp["DB Code"], "B-KHHO-0288")
        self.assertEqual(npp["Còn thiếu"], "SS Code, SS Name")
        self.assertEqual(npp["Số dòng"], 6)
        self.assertIn("KHA04", npp["Gợi ý"])
        self.assertIn("TU TAI", npp["Hiện có"])
        self.assertIn("chưa gán giám sát", npp["Nguyên nhân"])
        self.assertIn("không có trên cây", bosung.npp_cause("B-XXXX-0001", cfg))
        self.assertEqual(bosung.unit_cause("SP9", ""), "Dòng đơn hàng trên DMS không ghi ĐVT")
        self.assertIn("không còn trong danh mục", bosung.customer_cause("KH9", "Tỉnh", cfg))
        self.assertIn("để trống Tỉnh", bosung.customer_cause("KH1", "Tỉnh", {"customer_catalogue": {
            "ID1": {"customer_code": "KH1", "Tỉnh": ""}}}))
        self.assertEqual(frames["QuyDoi"].iloc[0]["ĐVT nguồn"], "Lốc")
        self.assertEqual(set(frames["SanPham"]["Mã sản phẩm"]), {"SP2"})
        summary = bosung.summary(frames)
        self.assertEqual(summary["NPP"]["missing"], {"SS Code": 1, "SS Name": 1})

    def test_user_file_fills_report_and_clears_todo(self):
        content = workbook({
            "NPP": [{"DB Code": "B-KHHO-0288", "Tên NPP": None, "SS Code": "KHA04", "SS Name": "Cường", "Vùng": None}],
            "QuyDoi": [{"Mã sản phẩm": "SP2", "ĐVT nguồn": "Lốc", "ĐVT báo cáo": "Thùng",
                        bosung.PER_PACK: 4},
                       {"Mã sản phẩm": "SP3", "ĐVT nguồn": "Gói", "ĐVT báo cáo": "Hộp", bosung.PER_PACK: 2}],
            "SanPham": [{"Mã sản phẩm": 530200025, "Brand": "Vikoda", "Package": "Vật phẩm"}],
        })
        overrides, problems = bosung.parse_overrides(content)
        self.assertEqual(len(problems), 1)  # "Hộp" is not a report pack unit
        self.assertIn("530200025", overrides["product_overrides"])
        cfg = bosung.apply_overrides(base_cfg(), overrides)
        cfg["products"]["SP2"] = {"Brand": "B", "Package": "P"}
        detail = pd.DataFrame([bill(), bill(stt="3", ma_sp="SP2", ten_dvt="Lốc", ma_dvt="Lốc", so_luong=8)],
                              dtype=object)
        report, issues = build_report(detail, cfg)
        self.assertTrue(issues.empty, issues.to_dict("records"))
        self.assertEqual(report.iloc[0]["SS Code"], "KHA04")
        self.assertEqual(report.iloc[1]["Số lượng SELL-OUT"], 2.0)
        stale = {"CTKM 2026-07": [{"_sheet": "NPP", "DB Code": "B-KHHO-0288", "Còn thiếu": ["SS Code"],
                                   "Số dòng": 3, "Nguồn": ["CTKM 2026-07"]}]}
        self.assertTrue(bosung.todo_frames(stale, overrides)["NPP"].empty)

    def test_publish_merges_state_creates_user_file_once_and_survives_lock(self):
        sp = Mock()
        sp.download_json.return_value = {"CTKM 2026-06": [{"_sheet": "SanPham", "Mã sản phẩm": "OLD",
                                                            "Còn thiếu": ["Package"], "Số dòng": 2,
                                                            "Nguồn": ["CTKM 2026-06"]}]}
        sp.download_file_bytes.return_value = None
        sp.get_item_by_path.return_value = None
        sp.upload_file.side_effect = [{"id": "x"}, RuntimeError("423 Locked")]
        rows = [{"_sheet": "NPP", "DB Code": "B-A-1", "Còn thiếu": ["SS Code"], "Số dòng": 1,
                 "Nguồn": ["CTKM 2026-07"]}]
        with tempfile.TemporaryDirectory() as folder:
            result = bosung.publish({"CTKM 2026-07": rows}, sp, "drive", output_dir=Path(folder))
            book = pd.read_excel(Path(folder) / bosung.USER_FILE, sheet_name=None)
        self.assertEqual(result["todo"]["NPP"]["keys"], 1)
        self.assertEqual(result["todo"]["SanPham"]["keys"], 1)
        self.assertTrue(result["user_file_created"])
        self.assertNotIn("todo_published", result)
        state = sp.upload_json.call_args.args[2]
        self.assertEqual(sorted(state), ["CTKM 2026-06", "CTKM 2026-07"])
        self.assertIn("HuongDan", book)
        self.assertEqual(list(book["NPP"]["DB Code"]), ["B-A-1"])

    def test_publish_never_raises(self):
        sp = Mock()
        sp.download_json.side_effect = RuntimeError("boom")
        self.assertIn("error", bosung.publish({}, sp, "drive"))


if __name__ == "__main__":
    unittest.main()
