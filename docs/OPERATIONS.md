# Runbook vận hành production

## Lịch và luồng tự động

Tất cả lịch nghiệp vụ dùng múi giờ `Asia/Ho_Chi_Minh`.

```text
HH:05 mỗi giờ       -> MobiWork DMS Sync: today
09:00 mỗi ngày      -> MobiWork DMS Sync: yesterday -> queue image sync
23:30 mỗi ngày      -> reconcile 7 completed days
02:00 Chủ nhật      -> full rebuild tháng hiện tại
02:30 ngày 1/tháng  -> full rebuild tháng trước để khóa sổ
11:30 mỗi ngày      -> production smoke
mỗi 2 giờ :20       -> operations health watchdog
```

Pipeline production kết thúc ở monthly master và ảnh gốc trên SharePoint. Không có bước chấm điểm ảnh hoặc tạo KPI nghiệp vụ.

## Monthly master

Mỗi report/tháng có một workbook canonical. Cột `_sync_date` xác định partition theo ngày và được ẩn trong Excel.

Pipeline incremental:

1. fetch source MobiWork;
2. validate required fields/business keys;
3. enrich Visit với Vùng theo mã nhân viên;
4. merge partition hiện tại;
5. cross-partition upsert theo `upsert_keys` khai báo trong `config/reports.json`;
6. staged SharePoint upload;
7. semantic verification;
8. promote file canonical hoặc giữ/rollback file an toàn khi lỗi.

Nếu canonical master chưa tồn tại, report được rebuild từ ngày 01 đến ngày mục tiêu. Nếu thiếu partition bắt buộc, pipeline không publish workbook chưa đầy đủ.

Phạm vi chạy thủ công:

- `today`: ngày hiện tại theo giờ Việt Nam;
- `yesterday`: ngày hôm qua;
- `lookback`: N ngày completed trước đó để correction/backfill.

## Business key và upsert

Production contract hiện tại:

- `visit`: partition replacement theo ngày, `upsert_keys=[]`;
- `new_customer`: `makh`;
- `order`: `ma_phieu`;
- `bill`: `ma_phieu`.

Không thêm heuristic mới vào `monthly_master.py`. Nếu thêm report mới cần cross-partition upsert, phải khai báo `upsert_keys` trong `reports.json` và thêm test.

## Vùng của Visit

`loai_kh` là phân loại khách hàng và **không được dùng làm Vùng**.

Visit production có:

- `vung_code`
- `vung`
- `vung_source`

Mapping nằm ở `config/employee_regions.json`, lấy từ prefix của `ma_nv`. Strict mode làm report fail nếu mã nhân viên chưa có mapping.

Consumer/Power BI phải filter Vùng bằng `vung`.

## Full-month rebuild

`MobiWork Full Month Rebuild` là đường khôi phục dữ liệu mạnh nhất.

Nó không đọc master cũ để làm source. Mỗi report được fetch lại từng ngày từ đầu tháng đến anchor.

Trước lần ghi SharePoint đầu tiên, **global source gate** yêu cầu tất cả report và tất cả ngày phải build local thành công. Nếu bất kỳ source report nào fail, toàn bộ publish set bị chặn và file SharePoint cũ được giữ nguyên.

Sau khi source gate pass, report được publish tuần tự. Nếu một SharePoint publish fail, các report phía sau bị chặn để giảm trạng thái nửa cũ/nửa mới.

Manual input:

```text
target_month = YYYY-MM
dry_run = false
```

Tháng hiện tại rebuild đến ngày hiện tại. Tháng quá khứ rebuild đến ngày cuối tháng.

## Đồng bộ ảnh

Image sync đọc metadata từ monthly master Viếng thăm trên SharePoint và lưu ảnh vào:

```text
Data anh/YYYY-MM/<Nhân viên>/<Mã KH>/...
```

Nó dùng `Data anh/_state.json`, one-day overlap, `retry_from_date`, giới hạn số ảnh mỗi batch và soft runtime budget. Khi còn mục tiêu chưa tải và batch vừa rồi có tiến triển, workflow tự gọi batch tiếp theo. URL lỗi được ghi trong manifest để production sau thử lại.

## Concurrency và an toàn

Report và image production dùng chung concurrency group `mobiwork-sharepoint-production` để tránh hai writer sửa SharePoint đồng thời.

- sync/image thường: `cancel-in-progress: false`;
- full-month rebuild: cùng production lock nhưng `cancel-in-progress: true` để recovery thủ công có thể ưu tiên.

Excel được so sánh theo nội dung worksheet thay vì chỉ dựa vào kích thước hoặc raw-file hash.

## Production smoke

`production-smoke.yml` fetch lại MobiWork cho ngày mục tiêu và so dữ liệu source sau transform với đúng partition trong monthly master SharePoint.

Nó kiểm cả image state. Với mismatch có thể sửa bằng reconciliation/image retry trong bounded one-shot recovery. Sau recovery, smoke chạy lại và workflow chỉ xanh khi consistency được xác nhận.

## Audit và giám sát

Report/rebuild ghi `output/sync_manifest.json`; image sync ghi `output/image_sync_manifest.json`.

Report manifest cần kiểm:

- `status`
- `failed_report_count`
- `source_row_count`
- `master_row_count`
- `sharepoint_write_count`
- `sharepoint_write_avoided_count`
- `verification_mode`
- `semantic_match`

Full rebuild còn có `source_gate_passed`, `days_expected`, `days_fetched`, `all_days_fetched`.

`operations-health.yml` kiểm độ mới của report sync, image sync và production smoke. Khi lỗi kéo dài, nó mở/cập nhật issue `[OPS] MobiWork automation unhealthy` và tự đóng khi phục hồi.

## Xử lý sự cố

1. Mở GitHub Actions Job Summary và xác nhận workflow + target date/month.
2. Nếu report lỗi, xem `sync_manifest.json` và `report_results`.
3. Nếu Visit lỗi mapping, cập nhật `config/employee_regions.json` đúng prefix nhân viên rồi chạy lại; không fallback bằng `loai_kh`.
4. Nếu dữ liệu một vài ngày sai/nhập trễ, chạy `lookback` phù hợp.
5. Nếu nghi master tháng đã thiếu hoặc tích lũy sai, chạy `MobiWork Full Month Rebuild` cho tháng đó.
6. Với ảnh, xem `image_sync_manifest.json`, đặc biệt `status`, `pending_remaining`, `failed_count`, `retry_from_date`.
7. Phân biệt lỗi dữ liệu cố định với timeout/rate limit/API tạm thời trước khi retry nhiều lần.
8. `dry_run=true` không ghi SharePoint và không được dùng khi mục tiêu là sửa dữ liệu production.

## Sau thay đổi schema/code

Trước merge:

```text
compile -> Ruff -> unit tests -> coverage -> CI green
```

Nếu thay đổi schema monthly master hoặc mapping Vùng, sau khi merge nên full rebuild tháng hiện tại; với tháng lịch sử bị ảnh hưởng thì rebuild chính tháng đó.

Secrets bắt buộc: `MOBIWORK_USER`, `MOBIWORK_TOKEN`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`.

Data contract chi tiết: `docs/DATA_CONTRACT.md`.
