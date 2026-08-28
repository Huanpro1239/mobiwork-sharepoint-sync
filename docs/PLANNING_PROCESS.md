# Quy trình vận hành Vikoda Planning Engine

Tài liệu này mô tả **quy trình chạy thực tế** của hệ thống kế hoạch đang nằm trong repository `mobiwork-sharepoint-sync`.

> Trạng thái hiện tại: **production-shadow V2**. GitHub/Python tự đọc dữ liệu thật trên SharePoint, tính kế hoạch và ghi kết quả shadow. File kế hoạch gốc `.xlsm` vẫn được giữ nguyên để đối chiếu/rollback và chưa bị Python ghi đè.

---

## 1. Sơ đồ tổng thể

```text
SharePoint nguồn
    |
    v
GitHub Actions
    |
    +--> Azure OIDC
    +--> Microsoft Graph
    |
    v
Python Planning Engine
    |
    +--> 9 bước refresh thay VBA Call_All
    +--> Forecast / tồn / nợ kho
    +--> BOM / MRP / nhu cầu NVL
    +--> ABC / MOQ / lead time / mua hàng
    +--> KHSX tuần
    +--> KHSX ngày KHS / PET / Galon / RGB
    +--> Phân bổ NVL theo ngày
    |
    v
Validation + manifest
    |
    v
SharePoint _PlanningEngine/shadow
    ├── planning_shadow.xlsx
    └── planning_manifest.json
```

---

## 2. Khi nào hệ thống chạy

Workflow: `.github/workflows/planning-engine.yml`

### Tự động theo lịch

Thứ Hai đến Thứ Bảy, giờ Việt Nam:

- 07:10
- 11:10
- 14:10
- 17:10

### Tự động khi thay đổi code production

Workflow chạy lại khi có `push` vào `main` và thay đổi một trong các vùng:

- `.github/workflows/planning-engine.yml`
- `config/planning_sources.json`
- `src/planning/**`
- `src/run_planning_engine.py`
- `src/sharepoint.py`

### Chạy thủ công

`workflow_dispatch` hỗ trợ `dry_run=true/false`.

- `dry_run=true`: tính thử nhưng không publish lên SharePoint.
- `dry_run=false`: tính và publish kết quả shadow lên SharePoint.

---

## 3. Nguồn dữ liệu

Master workbook:

```text
Tinh san xuat Mua hang 2027/
File tính kế hoạch - BẢN CẢI TIẾN_V2.xlsm
```

Các nguồn ngoài được khai báo tập trung trong `config/planning_sources.json`.

| Key | Vai trò |
|---|---|
| `ton_thuc_te_hien_tai` | Tồn thực tế hiện tại |
| `ton_vikoda` | Tồn Vikoda |
| `hang_nhap_truoc` | Hàng nhập trước / demand phục vụ nợ kho |
| `ton_vkd` | Tồn VKD |
| `ton_ban_duoc_vikoda` | Tồn bán được Vikoda |
| `ton_ban_duoc_vkd` | Tồn bán được VKD |
| `ton_nvl` | Tồn nguyên vật liệu |
| `xnt_vikoda` | XNT kế toán Vikoda |
| `xnt_vkd` | XNT kế toán VKD |
| `gui_kho_dau_thang` | Gửi kho đầu tháng |
| `gui_kho_hien_tai` | Gửi kho hiện tại |
| `ton_tt_dau_thang` | Tồn thực tế phục vụ KHSX |
| `ban_hang` | Báo cáo bán hàng |
| `ban_hang_vikoda` | Báo cáo bán hàng Vikoda |
| `xuat_kho` | Xuất kho ERP |
| `xuat_kho_vikoda` | Xuất kho ERP Vikoda |
| `po_mua_hang` | PO mua hàng đang mở |

**Nguyên tắc:** đường dẫn nguồn chỉ thay trong config; không hard-code thêm đường dẫn SharePoint vào business rule.

---

## 4. Quy trình kỹ thuật từng bước

### Bước 1 — GitHub chuẩn bị môi trường

GitHub Actions:

1. checkout `main`;
2. cài Python 3.12;
3. cài `requirements.txt`;
4. compile source planning để bắt lỗi syntax sớm.

Nếu bước này fail thì **không có dữ liệu nào trên SharePoint bị thay đổi**.

### Bước 2 — Đăng nhập Microsoft bằng OIDC

Workflow dùng:

```text
GitHub OIDC -> Microsoft Entra -> Microsoft Graph
```

Không lưu Microsoft client secret trong code.

Workflow resolve site:

```text
https://vikodacomvn.sharepoint.com/sites/Planning
```

sau đó tìm đúng document library có URL `/Shared Documents` và lấy `SHAREPOINT_DRIVE_ID`.

### Bước 3 — Entry point

Workflow chạy:

```bash
python src/run_planning_engine.py
```

Entry point tạo SharePoint client, đọc config và gọi `planning.engine.run_shadow()`.

### Bước 4 — Tải master + toàn bộ nguồn

`engine.py` tải master workbook và các source workbook qua Microsoft Graph.

Nếu bất kỳ nguồn bắt buộc nào không tồn tại hoặc không tải được, run phải fail thay vì tiếp tục với dữ liệu giả/0.

### Bước 5 — Chuẩn hóa dữ liệu

`normalize.py` chịu trách nhiệm:

- chuẩn hóa mã sản phẩm/NVL;
- xử lý số Excel;
- text tiếng Việt;
- các mapping mã `1 <-> 2` khi nghiệp vụ gốc yêu cầu;
- tránh làm business rule phụ thuộc kiểu dữ liệu ngẫu nhiên trong Excel.

### Bước 6 — Chạy 9 bước thay VBA `Call_All`

`vba_port.py` + `source_refresh.py` tái hiện chuỗi nguồn của VBA:

1. Bán hàng / FC tháng này.
2. Gửi kho.
3. Nợ kho.
4. PO mua hàng.
5. Tồn NVL.
6. Tính ứng hàng / tồn thành phẩm.
7. Tồn đầu tháng.
8. Xuất kho ERP tháng trước.
9. Tồn thực tế đưa vào kế hoạch sản xuất tuần.

Mục tiêu của lớp này là giữ **duplicate semantics, mã chuyển đổi và quy đổi đơn vị** giống workbook gốc.

### Bước 7 — Tính demand / tồn thành phẩm

`domain/demand.py` tính:

- FC hiện tại;
- FC M+1, M+2, M+3;
- đã bán;
- tồn Vikoda/VKD/nhà máy;
- còn lại;
- nợ kho;
- dự kiến nhu cầu vật tư.

Output chính trong workbook shadow:

```text
TinhUngHang_V2
```

### Bước 8 — BOM và MRP nguyên vật liệu

`domain/materials.py`:

1. chuẩn hóa `Flat BOM`;
2. chuẩn hóa direct BOM;
3. nhân nhu cầu thành phẩm với định mức;
4. tổng nhu cầu NVL theo kỳ;
5. ghép tồn NVL;
6. ghép PO mở;
7. tính lượng thiếu.

Output:

```text
KeHoachNhapNVL_V2
```

### Bước 9 — ABC và kế hoạch mua

`domain/purchasing.py` xử lý:

- ABC theo nhu cầu 3 tháng;
- rủi ro tồn/HSD;
- ngày thiếu dự kiến;
- lead time;
- MOQ;
- tồn + PO - nợ;
- mức độ khẩn cấp;
- lượng đề xuất mua;
- ngày mua hàng;
- ngày đặt mua.

Outputs:

```text
PhanTichABC_V2
MuaHang_V2
```

### Bước 10 — Tính kế hoạch sản xuất tuần

`domain/production.py` tính:

- tồn đầu thực tế;
- tồn đầu sổ sách;
- forecast tháng kế hoạch;
- tồn cuối dự kiến;
- nợ kho;
- số lượng cần sản xuất;
- làm tròn theo mẻ/ca;
- số ngày cần sản xuất;
- ngày bắt đầu sản xuất.

Output:

```text
KeHoachSXTuan_V2
```

### Bước 11 — Xếp kế hoạch sản xuất ngày

Scheduler hiện hỗ trợ:

- `KHS`
- `PET 9000`
- `Galon`
- `RGB`

`domain/production.py` xử lý KHS/PET/Galon.

`rgb_scheduler.py` xử lý dây RGB dùng chung, không cho hai mã RGB chồng công suất trong cùng khoảng thời gian và ghi rõ lượng chưa xếp nếu tháng không đủ capacity.

Output:

```text
KeHoachSXNgay_Auto
```

Kiểm tra bắt buộc:

```text
KHSX_ChuaAuto = 0 dòng
```

nếu toàn bộ mã cần sản xuất đã được scheduler hỗ trợ.

### Bước 12 — Phân bổ NVL theo lịch sản xuất ngày

`domain/materials.py` lấy trực tiếp `KeHoachSXNgay_Auto`, nhân Flat BOM và ghép ngày giao PO để xác định:

- nhu cầu NVL trong horizon;
- tồn đầu;
- PO mở;
- PO về trong kỳ;
- ngày thiếu đầu tiên;
- trạng thái;
- lượng cần mua thêm.

Output:

```text
PhanBoNVLNgay_V2
```

### Bước 13 — Tạo output workbook + manifest

Engine tạo:

```text
output/planning/planning_shadow.xlsx
output/planning/planning_manifest.json
```

Workbook shadow hiện có các bảng:

- `Stock_Reconciliation`
- `Sales_Actual_Cases`
- `Material_Stock`
- `PO_Open`
- `XuatKho_ThangTruoc`
- `TinhUngHang_V2`
- `KeHoachNhapNVL_V2`
- `PhanTichABC_V2`
- `MuaHang_V2`
- `KeHoachSXTuan_V2`
- `KeHoachSXNgay_Auto`
- `PhanBoNVLNgay_V2`
- `KHSX_ChuaAuto`

Manifest lưu các count quan trọng như:

- số mã sản phẩm;
- số dòng PO;
- số dòng ABC/mua hàng;
- số dòng KHSX tuần/ngày;
- số dòng RGB auto;
- số mã chưa auto;
- tháng/năm kế hoạch.

### Bước 14 — Publish an toàn lên SharePoint

Khi `DRY_RUN=false`, output được ghi vào:

```text
Tinh san xuat Mua hang 2027/_PlanningEngine/shadow/
```

File canonical:

```text
planning_shadow.xlsx
planning_manifest.json
```

Upload Excel dùng cơ chế staged replacement + semantic verification:

1. upload file tạm;
2. tải lại file SharePoint;
3. so sánh nội dung workbook theo sheet/cell;
4. chỉ sau khi verification pass mới promote thành tên canonical;
5. file cũ được giữ làm backup trong lúc promote;
6. backup chỉ xóa sau khi promote thành công.

Do SharePoint có thể repack `.xlsx`, không dùng kích thước byte làm tiêu chuẩn duy nhất.

### Bước 15 — Lưu artifact GitHub

Mỗi run lưu artifact `output/planning/` trong GitHub Actions 14 ngày để phục vụ điều tra và audit.

---

## 5. Quy trình kiểm tra một run

Khi kiểm tra nhanh một lần chạy, làm theo thứ tự sau:

1. Vào **Actions -> Vikoda Planning Engine**.
2. Run phải có trạng thái `Success`.
3. Kiểm tra step `Run planning shadow engine` = success.
4. Kiểm tra manifest có `status = shadow_v2`.
5. Kiểm tra `unsupported_weekly_rows = 0`.
6. Mở `planning_shadow.xlsx` trên SharePoint.
7. Kiểm tra tối thiểu 4 sheet:
   - `MuaHang_V2`
   - `KeHoachSXTuan_V2`
   - `KeHoachSXNgay_Auto`
   - `PhanBoNVLNgay_V2`
8. Nếu có thay đổi business rule, đối chiếu sample với workbook `.xlsm` trước khi coi là parity.

---

## 6. Khi run fail thì xử lý thế nào

### Fail trước khi upload

Không thay đổi file shadow đang dùng trên SharePoint. Đọc log GitHub để xác định nguồn/sheet/rule lỗi.

### Fail do source file đổi cấu trúc

Ví dụ:

- đổi tên sheet;
- đổi dòng header;
- đổi cột mã/số lượng;
- đổi đường dẫn file.

Không sửa trực tiếp trong `engine.py` nếu chỉ là đường dẫn. Ưu tiên:

1. cập nhật `config/planning_sources.json`;
2. nếu schema Excel thay đổi thật, sửa adapter `source_refresh.py`/`vba_port.py`;
3. thêm regression test;
4. CI xanh;
5. merge `main`.

### Fail ở business rule

Sửa đúng domain tương ứng:

```text
Demand          -> domain/demand.py
BOM / NVL / PO  -> domain/materials.py
ABC / mua hàng  -> domain/purchasing.py
KHSX            -> domain/production.py
RGB             -> rgb_scheduler.py
```

Không nhét fix tạm vào `formula_port.py` hoặc workflow YAML.

---

## 7. Quy trình thay đổi code chuẩn

```text
1. Tạo branch
2. Sửa đúng module nghiệp vụ
3. Thêm/chỉnh unit test
4. Push branch
5. CI: compile + Ruff + test + coverage
6. Review diff
7. Merge main
8. Planning Engine tự chạy shadow
9. Kiểm tra manifest + SharePoint output
10. Nếu parity ổn -> giữ thay đổi
```

Không sửa business logic trực tiếp trên `main` nếu thay đổi có rủi ro lớn.

---

## 8. Tiêu chí trước khi bỏ Excel/VBA hoàn toàn

Chỉ chuyển từ shadow sang canonical khi đạt đủ:

1. 9/9 refresh source ổn định.
2. MRP/mua hàng parity với workbook.
3. KHSX tuần parity hoặc có rule mới được nghiệp vụ chấp thuận.
4. KHSX ngày không còn mã unsupported.
5. Không có unexplained variance trong nhiều run liên tiếp; mục tiêu vận hành khuyến nghị là **10 run production liên tiếp**.
6. Có audit manifest và rollback rõ ràng.
7. Có cảnh báo khi nguồn thiếu hoặc schema thay đổi.
8. Có người nghiệp vụ xác nhận output Python là nguồn chuẩn.

Sau cutover:

```text
Python/GitHub = đầu não tính toán
SharePoint    = data hub + nơi xuất kết quả
Excel         = báo cáo / xem / in / nhập cấu hình có kiểm soát
VBA           = rollback lịch sử, không còn là runtime bắt buộc
```
