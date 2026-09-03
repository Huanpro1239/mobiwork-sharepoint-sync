# Data contract production

Tài liệu này là hợp đồng dữ liệu giữa MobiWork DMS, pipeline đồng bộ, SharePoint và các consumer như Power BI/dashboard.

## 1. Nguyên tắc chung

- Mỗi report có đúng một monthly master canonical cho mỗi tháng.
- `_sync_date` là partition ngày do pipeline quản lý; cột này được ẩn trong workbook nhưng không được xóa.
- `primary_key` dùng để kiểm tính hợp lệ/uniqueness của dữ liệu source trong một lần fetch.
- `upsert_keys` dùng để xác định bản ghi nào phải thay thế xuyên partition khi dữ liệu nghiệp vụ được cập nhật lại.
- Không suy luận business key từ tên cột. Mọi cross-partition upsert phải được khai báo trong `config/reports.json`.

## 2. Report contract

| Report | Canonical workbook | Export mode | Required / primary key | Cross-partition upsert |
|---|---|---|---|---|
| `visit` | `BaoCaoViengTham_YYYY-MM.xlsx` | `Data` | không áp business key cứng | không; refresh theo `_sync_date` |
| `new_customer` | `MoMoiKhachHang_YYYY-MM.xlsx` | `Data` | `makh` | `makh` |
| `order` | `DonDatHang_YYYY-MM.xlsx` | `DonHang` + `ChiTietSP` | `ma_phieu` | `ma_phieu` |
| `bill` | `DonBanHang_YYYY-MM.xlsx` | `DonHang` + `ChiTietSP` | `ma_phieu` | `ma_phieu` |

Với `order`/`bill`:

- `DonHang`: unique `ma_phieu` trong monthly master.
- `ChiTietSP`: unique `ma_phieu + stt`.
- `stt` thiếu/không hợp lệ được chuẩn hóa deterministic khi có thể; duplicate thực sự vẫn bị reject.

## 3. Visit và Vùng bán hàng

Các trường sau có ý nghĩa khác nhau:

- `loai_kh`: phân loại/segment khách hàng do source trả về.
- `vung_code`: mã Vùng bán hàng chuẩn do pipeline bổ sung.
- `vung`: tên Vùng bán hàng chuẩn do pipeline bổ sung.
- `vung_source`: nguồn mapping Vùng; production hiện dùng `ma_nv_prefix`.

Mapping được quản lý tại `config/employee_regions.json` theo prefix của `ma_nv`.

### Consumer rule bắt buộc

Power BI/dashboard phải dùng:

```text
Vùng = vung
```

Không được dùng:

```text
Vùng = loai_kh
```

Một khách hàng có thể mang `loai_kh` khác với Vùng phụ trách của nhân viên. Đây là tình huống hợp lệ và không phải lỗi sync.

Nếu `ma_nv` không map được sang Vùng, Visit fail ở strict mode. Mục tiêu là fail rõ ràng thay vì publish dữ liệu bị phân vùng sai.

## 4. Completeness và validation

Pipeline có nhiều lớp kiểm tra:

1. HTTP retry/backoff cho lỗi tạm thời và rate limit.
2. Required-field validation.
3. Primary-key uniqueness validation.
4. Với Bill: kiểm `API total == fetched raw rows`.
5. Monthly-master uniqueness validation sau merge.
6. Excel size validation.
7. Semantic verification sau staged SharePoint upload.
8. Production smoke fetch lại source và so partition SharePoint với dữ liệu MobiWork mới.

Không được tự thêm `total_path` cho endpoint nếu chưa xác nhận MobiWork thực sự trả field tổng tương ứng.

## 5. Recovery policy

### Incremental

- Theo giờ: `today`.
- 09:00: `yesterday`.
- Nightly: reconcile lại 7 ngày completed.

### Full rebuild

Full-month rebuild:

- không đọc monthly master cũ để làm source;
- fetch từng ngày từ ngày 01 đến anchor;
- build toàn bộ report ở local trước;
- chỉ bắt đầu ghi SharePoint khi tất cả report/tất cả ngày đã vượt source gate;
- dừng các publish phía sau nếu có SharePoint publish failure.

Lịch recovery:

- Chủ nhật 02:00: rebuild tháng hiện tại.
- Ngày 1 lúc 02:30: rebuild tháng trước để khóa sổ.

## 6. Audit

Report sync/rebuild ghi `output/sync_manifest.json` và upload audit JSON vào `_sync_runs/YYYY/MM/` khi SharePoint khả dụng.

Các trường cần theo dõi gồm:

- `status`
- `failed_report_count`
- `source_row_count`
- `master_row_count`
- `sharepoint_write_count`
- `sharepoint_write_avoided_count`
- `verification_mode`
- `semantic_match`
- `source_gate_passed` với full rebuild

## 7. Quy tắc thay đổi schema

Khi thêm/sửa report:

1. cập nhật `config/reports.json`;
2. khai báo `required_fields`, `primary_key`, `upsert_keys` khi có;
3. thêm/đổi test trong `tests/`;
4. chạy compile + Ruff + unit tests + coverage;
5. chỉ merge khi CI xanh;
6. nếu thay đổi schema workbook, rebuild tháng liên quan trước khi consumer refresh dashboard.
