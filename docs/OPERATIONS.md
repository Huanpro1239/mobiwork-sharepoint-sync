# Runbook vận hành production

## 1. Lịch chạy bình thường

`MobiWork DMS Sync` có hai lịch tự động theo múi giờ `Asia/Ho_Chi_Minh`:

```text
HH:05 mỗi giờ -> SYNC_SCOPE=today
09:00 mỗi ngày -> SYNC_SCOPE=yesterday
```

Các run theo lịch sử dụng `DRY_RUN=false`. Chọn phút `05` giúp giảm tải tại đúng đầu giờ.

Luồng production đầy đủ:

```text
Report refresh thành công
        │
        └─ nếu là refresh D-1 lúc 09:00
                 ▼
           Image reconciliation
                 │
                 ├─ warming_up -> tự chạy batch tiếp
                 └─ success + pending=0 + failed=0
                              ▼
                       Image AI + KPI
```

Run report theo giờ `today` chỉ cập nhật monthly master, không tự chấm KPI mỗi giờ.

## 2. Concurrency và queue

Report production và image reconciliation dùng chung concurrency group:

```text
mobiwork-sharepoint-production
```

Cấu hình:

```text
cancel-in-progress: false
queue: max
```

Mục tiêu là giữ các run quan trọng đang chờ thay vì để run mới thay thế pending run cũ. Các writer vẫn chạy tuần tự.

KPI dùng group riêng:

```text
mobiwork-kpi-production
```

để không có hai lần publish KPI production cùng lúc.

## 3. Cấu trúc monthly master

Mỗi report/tháng có đúng một workbook canonical:

```text
BaoCaoViengTham_YYYY-MM.xlsx
MoMoiKhachHang_YYYY-MM.xlsx
DonDatHang_YYYY-MM.xlsx
DonBanHang_YYYY-MM.xlsx
```

Cột `_sync_date` xác định partition theo ngày. Mỗi lần chạy bình thường:

1. tải monthly master hiện có;
2. gọi MobiWork cho ngày mục tiêu;
3. thay đúng partition `_sync_date` của ngày đó;
4. giữ nguyên các ngày còn lại;
5. ghi lại canonical file;
6. upload staged;
7. verify nội dung workbook;
8. chỉ sau khi verify đạt mới coi run là thành công.

Nếu canonical master bị thiếu, report được rebuild từ ngày 01 của tháng đến ngày mục tiêu. Rebuild đầu tiên có thể lâu hơn đáng kể so với update theo ngày.

## 4. Phạm vi incremental

```text
today      -> ngày hiện tại theo giờ Việt Nam
yesterday  -> ngày hôm qua theo giờ Việt Nam
lookback   -> N ngày trước, tối đa theo giới hạn runtime hiện hành
```

Tự động:

- hourly dùng `today`;
- 09:00 dùng `yesterday`.

`lookback` chỉ nên dùng cho correction/backfill có chủ đích.

## 5. Chính sách lỗi của report

Các report enabled chạy độc lập theo thứ tự cấu hình. Một report lỗi không chặn việc thu thập audit của các report còn lại.

Trạng thái tổng:

```text
mọi report thành công -> success
có report lỗi         -> partial_failure + workflow exit khác 0
lỗi setup/config      -> failed
```

Vì workflow report phải success mới dispatch image sync, monthly master nửa vời không được tự động đưa xuống AI/KPI.

## 6. Quy tắc dòng Order/Bill

`ChiTietSP` dùng `ma_phieu + stt` làm business key.

Dữ liệu lịch sử có thể thiếu hoặc sai `stt`. Xử lý production theo hướng bảo thủ:

1. giữ mọi `stt` số nguyên dương hợp lệ từ MobiWork;
2. chỉ dòng thiếu/sai mới được gán số nguyên dương chưa dùng theo thứ tự nguồn;
3. chạy kiểm tra duplicate key sau normalize;
4. không tự sửa im lặng hai `stt` hợp lệ nhưng trùng nhau.

Mục tiêu là không mất dòng nhưng vẫn phát hiện lỗi business key thật.

## 7. Verification file Excel trên SharePoint

SharePoint/Office có thể thay metadata trong OOXML nên size hoặc SHA-256 của toàn file `.xlsx` có thể khác dù dữ liệu sheet không đổi.

Vì vậy production verify Excel theo semantic content:

- tên và thứ tự worksheet;
- tọa độ mọi ô có dữ liệu;
- kiểu dữ liệu ô;
- giá trị ô.

Semantic mismatch phải fail closed.

JSON/audit file dùng kiểm tra byte/size bình thường. Canonical Excel được thay qua staged upload/promotion có rollback protection.

## 8. Đồng bộ ảnh

Image sync đọc metadata từ monthly master `visit` trên SharePoint, không tạo một nguồn metadata độc lập khác.

Thư mục ảnh:

```text
Data anh/YYYY-MM/<Nhân viên>/<Mã KH>/...
```

Identity ảnh:

```text
ngày nghiệp vụ + SHA256(URL MobiWork)
```

Cùng URL + cùng ngày được coi là một bằng chứng dù `stt_hinh` hoặc extension thay đổi. Cùng URL nhưng khác ngày phải được giữ thành hai target riêng.

Image sync sử dụng:

- `Data anh/_state.json`;
- one-day overlap cho incremental state;
- `retry_from_date` khi còn backlog;
- batch limit;
- soft runtime budget;
- cached folder listing để giảm số Graph call.

Trạng thái:

```text
success         -> reconcile hoàn tất
warming_up      -> không lỗi nguồn nhưng còn target deferred
partial_failure -> có target tải/ghi thất bại
failed          -> lỗi setup/source/storage
```

`warming_up` không được hiểu là “ảnh đã đủ”. Workflow tự dispatch batch tiếp nếu vẫn còn pending và batch vừa rồi có tiến triển.

## 9. Gate trước khi chạy KPI

`mobiwork-images.yml` chỉ dispatch production KPI khi manifest thỏa đồng thời:

```text
status == success
dry_run == false
pending_remaining == 0
failed_count == 0
```

Không sử dụng generic `workflow_run completed` để kích KPI, vì một workflow ảnh có thể kết thúc exit code 0 khi đang `warming_up` hoặc khi chạy dry-run.

## 10. Manual production refresh

### Dữ liệu hôm nay

```text
sync_scope=today
lookback_days=1
dry_run=false
```

### Chốt lại hôm qua

```text
sync_scope=yesterday
lookback_days=1
dry_run=false
```

### Correction/backfill

```text
sync_scope=lookback
lookback_days=N
dry_run=false
```

Sau khi manual report production thành công, workflow không chạy image sync inline nữa. Thay vào đó nó dispatch `mobiwork-images.yml` với `from_date` phù hợp với scope vừa refresh. Nhờ vậy manual production cũng có batch/resume và gate KPI giống lịch tự động.

Nếu chỉ kiểm tra không ghi SharePoint:

```text
dry_run=true
```

Dry-run report không dispatch image/KPI production.

## 11. KPI và image scoring

Production AI/KPI hiện chạy bằng `image-scoring-kpi.yml` trên GitHub-hosted Ubuntu runner.

Pipeline chấm ảnh có giới hạn số ảnh mỗi batch và soft runtime budget. Nếu AI còn backlog, trạng thái `warming_up` của KPI pipeline có thể tự dispatch batch chấm tiếp theo theo rule workflow.

Workbook KPI chỉ được publish sau fail-closed validation.

## 12. Audit manifest

Report run ghi:

```text
output/sync_manifest.json
```

và production upload audit lên:

```text
_sync_runs/YYYY/MM/<run_id>.json
```

Các counter quan trọng:

- `source_rows`: số dòng lấy cho ngày mục tiêu;
- `master_rows`: tổng dòng trong monthly master sau update;
- `source_row_count`: tổng source row của report thành công;
- `master_row_count`: tổng dòng master đã ghi.

Image run ghi:

```text
output/image_sync_manifest.json
```

Cần theo dõi:

- `candidate_count`;
- `unique_target_count`;
- `skipped_existing_count`;
- `uploaded_count`;
- `failed_count`;
- `deferred_count`;
- `pending_remaining`;
- `completeness_pct`;
- `retry_from_date`.

KPI run ghi:

```text
runtime/output/run_manifest.json
```

## 13. Failure behavior

Production phải fail an toàn với các lỗi integrity xác định được, ví dụ:

- MobiWork `status=false`;
- response thiếu cấu trúc mong đợi;
- pagination thiếu dòng khi API có `total`;
- thiếu required field;
- duplicate business key;
- workbook không hợp lệ;
- Graph upload lỗi sau retry;
- semantic verification mismatch;
- staged promotion không verify được;
- image source/storage lỗi;
- KPI workbook validation lỗi.

Timeout, rate limit và `5xx` tạm thời được retry có backoff. Không tăng retry chỉ để che lỗi schema/data deterministic.

## 14. Thứ tự xử lý khi production lỗi

1. Mở GitHub Actions Job Summary.
2. Xác nhận workflow và scope/ngày mục tiêu.
3. Với report: xem `report_results` và report nào failed.
4. Mở `sync_manifest.json`.
5. Với ảnh: mở `image_sync_manifest.json` và kiểm tra `status/pending/failed/retry_from_date`.
6. Với KPI: mở `run_manifest.json` và kiểm tra backlog/scoring/output validation.
7. Xem log step lỗi.
8. Phân biệt lỗi data contract với lỗi network/API tạm thời.
9. Chỉ kiểm tra Graph/OIDC/SharePoint sâu khi lỗi xảy ra ở giai đoạn storage/publish.

Report lỗi trước upload sẽ giữ monthly master SharePoint cũ không đổi.

## 15. Authentication

```text
GitHub Actions OIDC
    -> azure/login
    -> Azure CLI session
    -> AzureCliCredential
    -> Microsoft Graph
```

Secrets cần thiết:

```text
MOBIWORK_USER
MOBIWORK_TOKEN
AZURE_CLIENT_ID
AZURE_TENANT_ID
```

Không ghi secret thật vào source code, `.env.example`, log hoặc artifact.

## 16. Checklist trước khi thay production

1. CI phải xanh.
2. `mobiwork-sync.yml` vẫn có lịch hourly `5 * * * *` theo `Asia/Ho_Chi_Minh`.
3. D-1 finalization vẫn là `0 9 * * *` theo `Asia/Ho_Chi_Minh`.
4. Scheduled report vẫn `dry_run=false`.
5. Các report bắt buộc vẫn enabled.
6. Monthly partition replacement tests pass.
7. Semantic verification tests pass.
8. Workflow orchestration regression tests pass.
9. Image identity/date regression tests pass.
10. KPI không có generic `workflow_run` trigger từ image workflow.
11. Không có đường code production tạo lại legacy daily/history workbook.
12. Với thay đổi data contract, phải chạy validation trên dữ liệu thật trước khi coi issue là đóng.
