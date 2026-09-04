# Runbook vận hành production

## Bootstrap baseline trước khi bật lịch

Sau khi triển khai mới hoặc thay đổi logic dữ liệu quan trọng, bước production đầu tiên là chạy workflow **`MobiWork Bootstrap Full History`**. Không bật các workflow routine bằng tay trước khi bootstrap hoàn tất.

Input chuẩn hiện tại:

```text
start_month = 2026-06
end_month   = [để trống = tháng hiện tại]
dry_run     = false
```

`2026-06` là tháng lịch sử sớm nhất hiện đã xác nhận có file production trên SharePoint. Nếu MobiWork thực tế có dữ liệu trước tháng này thì phải đặt `start_month` sớm hơn.

Bootstrap production thực hiện theo thứ tự:

```text
wait active writer
        ↓
pause routine workflows
        ↓
2026-06 full rebuild
        ↓ pass toàn bộ report
2026-07 full rebuild
        ↓ pass toàn bộ report
2026-08 full rebuild
        ↓ pass toàn bộ report
tháng hiện tại → đến ngày hiện tại
        ↓
_sync_state/bootstrap.json = complete
        ↓
resume routine workflows
```

Quy tắc an toàn:

- bootstrap giữ shared production writer lock trong toàn bộ lần chạy và **không cancel** writer đang chạy;
- trước khi rebuild, nó disable `mobiwork-sync`, image sync, full-month recovery, nightly reconcile, historical reconcile, production smoke và operations health;
- mỗi tháng dùng full-month source gate: toàn bộ report và toàn bộ ngày của tháng đó phải build được trước khi publish set của tháng;
- nếu một tháng fail, các tháng sau không chạy;
- nếu bootstrap fail hoặc bị cancel, routine automation **vẫn bị disable**;
- chỉ khi tất cả tháng thành công, state được ghi `status=complete`, `bootstrap_complete=true` và workflow routine mới được enable lại;
- `dry_run=true` không ghi SharePoint và không pause/resume automation.

Nếu bootstrap fail, sửa nguyên nhân rồi chạy lại bootstrap. Không manually enable các workflow routine chỉ để “chạy tiếp”, vì như vậy có thể tạo baseline lịch sử chưa đầy đủ.

## Lịch và luồng tự động sau bootstrap

Tất cả lịch nghiệp vụ dùng múi giờ `Asia/Ho_Chi_Minh`.

```text
HH:05 mỗi giờ       -> MobiWork DMS Sync: today
09:00 mỗi ngày      -> MobiWork DMS Sync: yesterday -> queue image sync
23:30 mỗi ngày      -> reconcile 14 completed days
02:00 Chủ nhật      -> full rebuild tháng hiện tại
05:00 Chủ nhật      -> full rebuild tháng trước
03:30 ngày 2/tháng  -> full rebuild tháng trước để khóa sổ
04:30 ngày 3/tháng  -> reconcile toàn bộ lịch sử đã hoàn tất từ 2026-06
11:30 mỗi ngày      -> production smoke
mỗi 2 giờ :20       -> operations health watchdog
```

Pipeline production kết thúc ở monthly master và ảnh gốc trên SharePoint. Không có bước chấm điểm ảnh hoặc tạo KPI nghiệp vụ.

## Monthly master

Mỗi report/tháng có một workbook canonical. Cột `_sync_date` xác định partition theo ngày và được ẩn trong Excel.

Pipeline incremental:

1. fetch source MobiWork;
2. với paginated report: tiếp tục đến `API total` hoặc page rỗng; không coi page ngắn là EOF;
3. reject API repeated-page thay vì loop hoặc ghi dữ liệu trùng/thiếu;
4. validate required fields/business keys;
5. enrich Visit với Vùng theo mã nhân viên;
6. merge partition hiện tại;
7. cross-partition upsert theo `upsert_keys` khai báo trong `config/reports.json`;
8. chạy partition quality gate để xác nhận dữ liệu vừa fetch tồn tại đầy đủ trong master kết quả;
9. staged SharePoint upload;
10. semantic verification;
11. promote file canonical hoặc giữ/rollback file an toàn khi lỗi.

Với `order`/`bill`, quality gate kiểm cả `DonHang[ma_phieu]`, `ChiTietSP[ma_phieu,stt]` và không cho detail cũ của một `ma_phieu` đã được thay thế còn sót trong master.

Nếu canonical master chưa tồn tại, report được rebuild từ ngày 01 đến ngày mục tiêu. Nếu thiếu partition bắt buộc, pipeline không publish workbook chưa đầy đủ.

Phạm vi chạy thủ công:

- `today`: ngày hiện tại theo giờ Việt Nam;
- `yesterday`: ngày hôm qua;
- `lookback`: N ngày completed trước đó để correction/backfill.

## Business key và upsert

Production contract hiện tại:

- `visit`: partition replacement theo ngày, `upsert_keys=[]`;
- `new_customer`: `ID` của record MobiWork;
- `order`: `ma_phieu`;
- `bill`: `ma_phieu`.

`makh` của `new_customer` không phải khóa unique. Nếu source có hai record khác `ID` nhưng cùng `makh`, phải giữ cả hai. Không đổi lại primary/upsert key về `makh` chỉ để “lọc trùng”, vì dữ liệu lịch sử thực tế có reused customer code.

Không thêm heuristic mới vào `monthly_master.py`. Nếu thêm report mới cần cross-partition upsert, phải khai báo `upsert_keys` trong `reports.json` và thêm test.

## Vùng của Visit

`loai_kh` là phân loại khách hàng và **không được dùng làm Vùng**.

Visit production có:

- `vung_code`
- `vung`
- `vung_source`

Mapping nằm ở `config/employee_regions.json`, lấy từ prefix của `ma_nv`. Strict mode làm report fail nếu mã nhân viên chưa có mapping.

Consumer/Power BI phải filter Vùng bằng `vung`.

Trong bootstrap lịch sử, nếu một prefix nhân viên cũ chưa có mapping thì bootstrap phải fail an toàn. Cập nhật mapping đúng nghiệp vụ rồi chạy lại; không fallback sang `loai_kh`.

## Full-month rebuild

`MobiWork Full Month Rebuild` là đường khôi phục một tháng riêng lẻ.

Nó không đọc master cũ để làm source. Mỗi report được fetch lại từng ngày từ đầu tháng đến anchor.

Trước lần ghi SharePoint đầu tiên, **global source gate** yêu cầu tất cả report và tất cả ngày phải build local thành công. Nếu bất kỳ source report nào fail, toàn bộ publish set bị chặn và file SharePoint cũ được giữ nguyên.

Sau khi source gate pass, report được publish tuần tự. Nếu một SharePoint publish fail, các report phía sau bị chặn để giảm trạng thái nửa cũ/nửa mới.

Manual input:

```text
target_month = YYYY-MM
dry_run = false
```

Tháng hiện tại rebuild đến ngày hiện tại. Tháng quá khứ rebuild đến ngày cuối tháng.

## Historical reconciliation hàng tháng

`MobiWork Historical Reconciliation` chạy `04:30` ngày 3 hàng tháng. Mục tiêu là bắt các chỉnh sửa/back-date nằm ngoài cửa sổ nightly 14 ngày và ngoài current/previous-month weekly recovery.

Mặc định workflow rebuild tuần tự:

```text
2026-06 -> 2026-07 -> ... -> tháng trước
```

Mỗi tháng dùng đúng full-month source gate như manual rebuild. Nếu một tháng fail, các tháng sau không chạy trong lần đó. Workflow này **không thay đổi** `_sync_state/bootstrap.json`; nó chỉ yêu cầu bootstrap baseline đã ready trước khi chạy production.

Có thể chạy thủ công với:

```text
start_month = 2026-06
end_month   = [trống = tháng trước]
dry_run     = false
```

## Đồng bộ ảnh

Image sync đọc metadata từ monthly master Viếng thăm trên SharePoint và lưu ảnh vào:

```text
Data anh/YYYY-MM/<Nhân viên>/<Mã KH>/...
```

Nó dùng `Data anh/_state.json`, one-day overlap, `retry_from_date`, giới hạn số ảnh mỗi batch và soft runtime budget. Khi còn mục tiêu chưa tải và batch vừa rồi có tiến triển, workflow tự gọi batch tiếp theo. URL lỗi được ghi trong manifest để production sau thử lại.

## Concurrency và an toàn

Report, image, full-month rebuild, historical reconciliation và bootstrap dùng chung concurrency group `mobiwork-sharepoint-production` để tránh hai writer sửa SharePoint đồng thời.

Tất cả writer dùng `cancel-in-progress: false`. Recovery/rebuild phải **chờ** writer hiện tại hoàn tất; không cắt ngang một job đang publish vì điều đó có thể để một số report đã mới trong khi report khác vẫn cũ.

Bootstrap còn pause routine workflows sau khi nó lấy được production lock, và chỉ resume khi historical baseline hoàn tất thành công.

Excel được so sánh theo nội dung worksheet thay vì chỉ dựa vào kích thước hoặc raw-file hash.

## Production smoke

`production-smoke.yml` fetch lại MobiWork cho ngày mục tiêu và so dữ liệu source sau transform với đúng partition trong monthly master SharePoint.

Nó kiểm cả image state. Với mismatch có thể sửa bằng reconciliation/image retry trong bounded one-shot recovery. Sau recovery, smoke chạy lại và workflow chỉ xanh khi consistency được xác nhận.

## Audit và giám sát

Report/rebuild/bootstrap/historical reconciliation ghi `output/sync_manifest.json`; image sync ghi `output/image_sync_manifest.json`.

Bootstrap còn ghi readiness state tại:

```text
_sync_state/bootstrap.json
```

Trạng thái cho phép schedule tiếp tục là:

```json
{
  "status": "complete",
  "bootstrap_complete": true
}
```

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

Bootstrap có `months_expected`, `months_completed`, `month_count_expected`, `month_count_completed`, `failed_month`, `bootstrap_complete`.

Historical reconciliation có `months_expected`, `months_completed`, `month_count_expected`, `month_count_completed`, `failed_month`, `history_reconcile_complete`.

`operations-health.yml` kiểm độ mới của report sync, image sync và production smoke. Khi lỗi kéo dài, nó mở/cập nhật issue `[OPS] MobiWork automation unhealthy` và tự đóng khi phục hồi.

## Xử lý sự cố

1. Nếu đang bootstrap, xem Job Summary và `sync_manifest.json`; xác định `failed_month` và report lỗi.
2. Nếu Visit lỗi mapping, cập nhật `config/employee_regions.json` đúng prefix nhân viên rồi chạy lại; không fallback bằng `loai_kh`.
3. Nếu `new_customer` báo duplicate `makh`, không được loại record hoặc ép `makh` thành unique; kiểm `ID` của source vì `ID` mới là identity chuẩn.
4. Khi bootstrap chưa complete, giữ routine workflows ở trạng thái disabled.
5. Sau bootstrap, nếu dữ liệu một vài ngày sai/nhập trễ, chạy `lookback` phù hợp.
6. Nếu nghi một master tháng đã thiếu hoặc tích lũy sai, chạy `MobiWork Full Month Rebuild` cho tháng đó.
7. Nếu nghi chỉnh sửa cũ hơn tháng trước không được bắt, chạy `MobiWork Historical Reconciliation` từ tháng lịch sử cần kiểm tra.
8. Nếu API pagination báo repeated page hoặc total mismatch, không bỏ qua gate; kiểm source/API trước khi cho publish.
9. Với ảnh, xem `image_sync_manifest.json`, đặc biệt `status`, `pending_remaining`, `failed_count`, `retry_from_date`.
10. Phân biệt lỗi dữ liệu cố định với timeout/rate limit/API tạm thời trước khi retry nhiều lần.
11. `dry_run=true` không ghi SharePoint và không được dùng khi mục tiêu là sửa dữ liệu production.

## Sau thay đổi schema/code

Trước merge:

```text
compile -> Ruff -> unit tests -> coverage -> CI green
```

Nếu thay đổi schema monthly master hoặc mapping Vùng, production baseline nên được bootstrap/rebuild lại phạm vi tháng bị ảnh hưởng trước khi dashboard refresh.

Secrets bắt buộc: `MOBIWORK_USER`, `MOBIWORK_TOKEN`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`.

Data contract chi tiết: `docs/DATA_CONTRACT.md`.
