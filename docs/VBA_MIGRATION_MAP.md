# VBA migration map — File tính kế hoạch BẢN CẢI TIẾN V2

## Workbook profile

- 25 worksheets, including hidden/helper sheets.
- khoảng 40 VBA modules/classes trong `vbaProject.bin`.
- `Call_All.ChayTuDong_PAD` điều phối 9 bước VBA chính.
- Business logic lịch sử bị chia giữa VBA và hàng nghìn formula Excel, đặc biệt ở `Mua hang`, `Phan tich ABC`, `Phan bo NVL ngay`, `Ke hoach SX tuan`.

## 9 bước VBA chính -> Python

| # | VBA procedure | Nguồn / đích chính | Python hiện tại | Trạng thái |
|---|---|---|---|---|
| 1 | `TongHopDuLieu_SP_FC_ThangNay` | sales -> `FC thang nay` | sales adapters + `vba_port` | Auto |
| 2 | `GuiKho_ChayCaHai` | gửi kho -> `FC thang nay` | `aggregate_gui_kho` + code map | Auto |
| 3 | `Run_Nokho_Complete` | tồn/demand -> `Nokho` | `nokho_col_d/e` + `nokho_balance` | Auto |
| 4 | `Lay_PO` | `REPORT_DONMUAHANG` -> `PO` | `extract_open_po` | Auto |
| 5 | `CapNhatCotD_KeHoachNhapNVL_TuB8` | `Ton VT.xlsx` -> tồn NVL | `material_stock_last` | Auto |
| 6 | `Run_TinhUngHang_AllInOne` | tồn Vikoda/VKD/bán được | divided-stock functions | Auto |
| 7 | `Run_FCThangNay_ColM_SumAOAP` | XNT -> FC tồn đầu tháng | `sum_two_divided_stocks` | Auto |
| 8 | `LayDuLieuXuatKho` | ERP outbound | `aggregate_xuat_kho` | Auto |
| 9 | `Run_Update_TonTT_To_KHSX_Tuan` | tồn thực tế -> KHSX tuần | `map_ton_tt` | Auto |

## Formula Excel -> Python domain

| Workbook logic | Python module | Trạng thái |
|---|---|---|
| FC tháng / projection thành phẩm | `domain/demand.py` | Auto shadow |
| `Tinh ung hang` | `domain/demand.py` + source reconciliation | Auto shadow |
| Flat BOM / direct BOM | `domain/materials.py` | Auto shadow |
| `Ke hoach nhap NVL` | `domain/materials.py` | Auto shadow |
| `Phan tich ABC` | `domain/purchasing.py` | Auto shadow |
| `Mua hang` | `domain/purchasing.py` | Auto shadow |
| `Ke hoach SX tuan` | `domain/production.py` | Auto shadow |
| KHS/PET/Galon daily schedule | `domain/production.py` | Auto shadow |
| RGB daily schedule | `rgb_scheduler.py` | Auto shadow |
| `Phan bo NVL ngay` | `domain/materials.py` | Auto shadow |

`formula_port.py` hiện chỉ là compatibility facade để giữ các import cũ. Logic mới không được thêm vào file này.

## VBA/Excel còn giữ vai trò gì

- Workbook `.xlsm`: nguồn cấu hình, đối chiếu parity và rollback.
- Dashboard/print/layout VBA: presentation legacy; chưa cần thiết cho calculation engine.
- Các ô lịch nhập tay cũ: comparison-only; scheduler Python sinh lịch riêng trong shadow output.

## Duplicate / mapping semantics phải giữ

Một số rule lịch sử cố ý khác nhau:

- có nguồn **SUM** duplicate;
- có nguồn lấy **first occurrence**;
- có nguồn lấy **last occurrence**;
- một số luồng cần chuyển mã `1 <-> 2`;
- quy đổi DMSP divisor là business rule, không chỉ là format.

Các semantics này phải được khóa bằng regression test, không được “dọn cho đẹp” nếu làm thay đổi kết quả.

## Rủi ro kỹ thuật đã loại bỏ

1. Không còn phụ thuộc `Workbooks.Open(https://...)` để engine chạy headless.
2. GitHub Actions dùng Microsoft Graph/OIDC thay cho Office desktop session.
3. Upload `.xlsx` dùng semantic verification vì SharePoint có thể repack OOXML.
4. Business logic đã có unit/regression tests thay vì chỉ dựa vào macro message box.
5. Đường dẫn nguồn tập trung trong `config/planning_sources.json`.

## Rủi ro còn lại trước cutover canonical

1. Cần tiếp tục parity nghiệp vụ qua nhiều run production.
2. Một số rule scheduler Python là rule minh bạch mới, không phải copy nguyên lịch nhập tay cũ; cần người nghiệp vụ xác nhận.
3. Cần alert rõ khi source workbook đổi schema.
4. Cần chốt owner/phê duyệt trước khi Python được phép ghi canonical result thay workbook hiện tại.

## Target state

```text
Python/GitHub = đầu não tính toán
SharePoint    = nguồn dữ liệu + nơi publish kết quả
Excel         = giao diện xem/in/cấu hình có kiểm soát
VBA           = rollback lịch sử, không còn runtime bắt buộc
```

Quy trình chi tiết: [`PLANNING_PROCESS.md`](PLANNING_PROCESS.md).
