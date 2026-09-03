# Runbook vận hành production

## Lịch và luồng tự động

`MobiWork DMS Sync` chạy theo múi giờ `Asia/Ho_Chi_Minh`:

```text
HH:05 mỗi giờ  -> cập nhật dữ liệu hôm nay
09:00 mỗi ngày -> chốt dữ liệu hôm qua -> gọi đồng bộ ảnh
23:30 mỗi ngày -> đối soát lại ba ngày gần nhất -> gọi đồng bộ ảnh
```

Luồng production kết thúc sau khi monthly master và ảnh gốc được cập nhật trên SharePoint. Không có bước chấm điểm ảnh hoặc tạo KPI.

## Monthly master

Mỗi báo cáo/tháng có một workbook canonical. Cột `_sync_date` xác định partition theo ngày. Pipeline tải master hiện có, thay đúng partition, giữ các ngày khác, staged upload và kiểm tra lại nội dung trước khi hoàn tất.

Nếu canonical master chưa tồn tại, báo cáo được rebuild từ ngày 01 đến ngày mục tiêu. Nếu thiếu partition bắt buộc, pipeline không publish workbook chưa đầy đủ.

Phạm vi chạy:

- `today`: ngày hiện tại theo giờ Việt Nam;
- `yesterday`: ngày hôm qua;
- `lookback`: N ngày trước để correction/backfill.

## Đồng bộ ảnh

Image sync đọc metadata từ monthly master viếng thăm trên SharePoint và lưu ảnh vào:

```text
Data anh/YYYY-MM/<Nhân viên>/<Mã KH>/...
```

Nó dùng `Data anh/_state.json`, one-day overlap, `retry_from_date`, giới hạn số ảnh mỗi batch và soft runtime budget. Khi còn mục tiêu chưa tải và batch vừa rồi có tiến triển, workflow tự gọi batch tiếp theo. URL lỗi vẫn được ghi trong manifest để lần production sau thử lại; chúng không kích hoạt bất kỳ luồng xử lý ảnh nào khác.

## Concurrency và an toàn

Report và image production dùng chung concurrency group `mobiwork-sharepoint-production` với `cancel-in-progress: false`, tránh hai writer sửa SharePoint cùng lúc. Excel được so sánh theo nội dung worksheet thay vì chỉ dựa vào kích thước hoặc hash của toàn file.

## Audit và giám sát

Report ghi `output/sync_manifest.json`; image sync ghi `output/image_sync_manifest.json`. Các trường ảnh quan trọng là `uploaded_count`, `failed_count`, `deferred_count`, `pending_remaining`, `completeness_pct` và `retry_from_date`.

`operations-health.yml` kiểm tra độ mới của report sync, image sync và production smoke; khi có lỗi kéo dài, nó mở/cập nhật issue `[OPS] MobiWork automation unhealthy` và tự đóng khi phục hồi.

## Xử lý sự cố

1. Mở GitHub Actions Job Summary và xác nhận workflow, scope/ngày mục tiêu.
2. Với báo cáo, xem `sync_manifest.json` và `report_results`.
3. Với ảnh, xem `image_sync_manifest.json`, đặc biệt `status/pending/failed/retry_from_date`.
4. Phân biệt lỗi dữ liệu cố định với timeout/rate limit/API tạm thời.
5. Chạy thủ công `today`, `yesterday` hoặc `lookback` nếu cần sửa dữ liệu; dry-run không ghi SharePoint và không gọi image production.

Secrets bắt buộc: `MOBIWORK_USER`, `MOBIWORK_TOKEN`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`.
