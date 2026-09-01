# Tối ưu vận hành production

Tài liệu này mô tả các lớp vận hành bổ sung cho pipeline MobiWork → SharePoint.

## 1. No-op Excel upload

`SemanticSharePointClient` so sánh nội dung nghiệp vụ của workbook mới với workbook đang có trên SharePoint trước khi staged replace.

Nếu hai workbook giống nhau về:

- tên/thứ tự worksheet;
- tọa độ ô có dữ liệu;
- kiểu dữ liệu;
- giá trị ô;

thì upload được bỏ qua và trả về:

```text
verification_mode = xlsx_semantic_noop
semantic_match    = true
upload_skipped    = true
```

Mục tiêu là giảm Graph API write, rename, backup/delete và verify cycle khi run theo giờ không tạo ra thay đổi dữ liệu thật.

Nếu bước semantic comparison gặp lỗi, client không coi workbook là giống nhau. Pipeline tự quay về staged replacement hiện hữu để ưu tiên tính đúng dữ liệu.

## 2. Batch lookback theo report/tháng

Lookback nhiều ngày không còn xử lý mỗi ngày như một vòng SharePoint độc lập.

Luồng mới:

```text
một report + một tháng
        │
        ├─ download canonical monthly master 1 lần
        ├─ fetch từng target date từ MobiWork
        ├─ merge các partition thành công trong RAM
        └─ write/publish canonical monthly master tối đa 1 lần
```

Ví dụ `lookback_days=3` và cả D-1..D-3 cùng nằm trong một tháng:

```text
trước: 3 download + 3 publish / report
sau  : 1 download + 1 publish / report
```

Nếu lookback cắt qua ranh giới tháng, mỗi report có tối đa một read/publish cho từng tháng liên quan.

Khi canonical master đã tồn tại, lỗi fetch của một target date không chặn các target date khác trong cùng tháng. Ngày lỗi được ghi `failed`, các ngày lấy được dữ liệu vẫn được merge và publish chung.

Khi canonical master chưa tồn tại, hệ thống phải rebuild từ ngày 01 đến target mới nhất của tháng. Nếu thiếu bất kỳ partition bắt buộc nào trong rebuild, batch fail-closed để không publish một monthly master thiếu dữ liệu.

## 3. Nightly reconciliation

Workflow:

```text
.github/workflows/nightly-reconcile.yml
```

Lịch mặc định:

```text
23:30 Asia/Ho_Chi_Minh mỗi ngày
```

Workflow dispatch `mobiwork-sync.yml` với:

```text
sync_scope    = lookback
lookback_days = 3
dry_run        = false
```

Mục tiêu là tự bắt các thay đổi muộn trong D-1..D-3 mà không cần chạy backfill tay. Batch report/tháng giúp reconciliation này không nhân số lần SharePoint I/O theo số ngày.

Sau khi report reconciliation thành công, orchestration hiện hữu của `mobiwork-sync.yml` tiếp tục queue image reconciliation từ ngày sớm nhất của lookback. KPI chỉ chạy sau image gate production như trước.

Có thể chạy workflow thủ công với `lookback_days` từ 1 đến 31 khi cần correction lớn hơn.

## 4. Operations health watchdog

Workflow:

```text
.github/workflows/operations-health.yml
```

Lịch mặc định:

```text
mỗi 2 giờ, phút 20
```

Health thresholds:

```text
MobiWork DMS Sync : phải có success trong 150 phút gần nhất
MobiWork Images   : phải có success trong 36 giờ gần nhất
```

Nếu một threshold bị vi phạm, watchdog mở hoặc cập nhật một incident duy nhất:

```text
[OPS] MobiWork automation unhealthy
```

Khi pipeline phục hồi và cả hai threshold đạt lại, watchdog comment trạng thái recovery rồi tự đóng incident.

Mục tiêu là phát hiện trường hợp pipeline dừng hoặc fail kéo dài mà không cần người vận hành thường xuyên mở GitHub Actions kiểm tra.

## 5. Metrics trong sync manifest

`sync_manifest.json` có các counter để đo trực tiếp hiệu quả vận hành:

```text
target_execution_count          số report-date cần xử lý
workbook_group_count            số workbook report/tháng thực sự được chuẩn bị
sharepoint_write_count          số lần ghi SharePoint thực tế
sharepoint_write_avoided_count  số lần ghi tránh được nhờ semantic no-op
upload_skipped_count            số workbook trả về xlsx_semantic_noop
source_row_count                tổng dòng nguồn của target date thành công
master_row_count                tổng dòng của các monthly master vật lý được chuẩn bị
```

Với lookback nhiều ngày, `master_row_count` chỉ tính một lần cho mỗi workbook report/tháng thay vì lặp lại cùng một monthly master theo từng target date.

## 6. KPI vận hành đề xuất

Theo dõi theo tuần:

| KPI | Mục tiêu |
|---|---:|
| Scheduled report success rate | >= 99% |
| Daily image reconciliation success | >= 99% |
| Stale pipeline incident | 0 kéo dài > 4 giờ |
| Manual backfill D-1..D-3 | gần 0 |
| SharePoint writes / target executions | giảm theo batch/no-op |
| `xlsx_semantic_noop` trên run không đổi | càng cao càng tốt |

`xlsx_semantic_noop` cao không phải lỗi; nó cho thấy pipeline vẫn kiểm tra dữ liệu thường xuyên nhưng tránh ghi SharePoint khi nội dung không đổi.

## 7. Khi incident xuất hiện

Kiểm tra theo thứ tự:

1. mở run link được watchdog ghi trong issue;
2. xác định lỗi nằm ở MobiWork credentials/API, Microsoft OIDC/Graph, SharePoint library hay image pipeline;
3. sửa nguyên nhân gốc;
4. chạy manual `MobiWork DMS Sync` với scope phù hợp nếu cần;
5. không đóng incident bằng tay chỉ để làm sạch danh sách — watchdog sẽ tự đóng khi health thực sự phục hồi.
