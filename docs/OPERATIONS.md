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
- trước khi rebuild, nó disable `mobiwork-sync`, Data chấm ảnh backfill, full-month recovery, nightly reconcile, historical reconcile, production smoke và operations health;
- mỗi tháng dùng full-month source gate: toàn bộ report và toàn bộ ngày của tháng đó phải build được trước khi publish set của tháng;
- nếu một tháng fail, các tháng sau không chạy;
- nếu bootstrap fail hoặc bị cancel, routine automation **vẫn bị disable**;
- chỉ khi tất cả tháng thành công, state được ghi `status=complete`, `bootstrap_complete=true` và workflow routine mới được enable lại;
- `dry_run=true` không ghi SharePoint và không pause/resume automation.

Nếu bootstrap fail, sửa nguyên nhân rồi chạy lại bootstrap. Không manually enable các workflow routine chỉ để “chạy tiếp”, vì như vậy có thể tạo baseline lịch sử chưa đầy đủ.

## Lịch và luồng tự động sau bootstrap

Tất cả lịch nghiệp vụ dùng múi giờ `Asia/Ho_Chi_Minh`.

```text
07:05-19:05 mỗi 3 giờ, T2-T7 -> MobiWork DMS Sync: today
09:00 mỗi ngày               -> MobiWork DMS Sync: yesterday + Data chấm ảnh
23:30 mỗi ngày               -> reconcile 3 completed days (D-1..D-3)
02:00 Chủ nhật               -> full rebuild tháng hiện tại
03:30 ngày 2/tháng           -> full rebuild tháng trước để khóa sổ
10:15 thứ 2                  -> Data chấm ảnh backfill
11:30 mỗi ngày               -> production smoke
08:20 mỗi ngày               -> operations health watchdog
thủ công                     -> historical reconcile, full-month rebuild, bootstrap
```

GitHub chỉ giữ một run đang chờ trong concurrency group `mobiwork-sharepoint-production`; run đang chờ cũ hơn sẽ bị hủy khi có run mới xếp hàng. Watchdog cảnh báo nếu full-month rebuild gần nhất không thành công, nên một lần rebuild khóa sổ bị hủy sẽ không bị bỏ sót.

Pipeline production kết thúc ở monthly master của 4 báo cáo lịch sử, workbook Data chấm ảnh và snapshot Báo cáo trả thưởng trên SharePoint. Không tải/copy file ảnh, không chấm điểm ảnh và không tạo KPI nghiệp vụ.

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

`MobiWork Historical Reconciliation` chỉ chạy thủ công. Mục tiêu là bắt các chỉnh sửa/back-date nằm ngoài cửa sổ nightly 3 ngày, ngoài weekly rebuild tháng hiện tại và ngoài lần khóa sổ tháng trước.

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

## Data chấm ảnh

`run_data_cham_anh.py` (sau mỗi lần report sync) và `run_data_cham_anh_backfill.py` (rebuild tháng, historical, bootstrap, backfill thứ 2) đọc monthly master Viếng thăm + Đơn bán hàng trên SharePoint và ghi:

```text
05_DataChamAnh/YYYY/MM/Data_cham_anh_YYYY-MM.xlsx
```

Sheet `Data_anh` có một dòng cho mỗi link ảnh viếng thăm (link gốc `hinh_anh` của MobiWork, không copy ảnh). Sheet `Data_don_hang` là chi tiết đơn bán hàng. Workbook không đổi nội dung thì không ghi lại.

## Báo cáo trả thưởng

Trong mỗi lần `MobiWork DMS Sync`, sau khi luồng report chính và Data chấm ảnh hoàn tất, `src/promotion_bonus.py` refresh snapshot:

```text
06_BaoCaoTraThuong/BaoCaoTraThuong_Current.xlsx
```

Luồng thực hiện:

1. phân trang `/OpenAPI/V1/PromotionBonus` với `page_size <= 200`;
2. yêu cầu mỗi chương trình có `_id`;
3. gọi `/OpenAPI/V1/PromotionBonusReport?id_ct=<id>` riêng cho từng chương trình;
4. kiểm `total == len(data)` khi API trả `total`;
5. build 4 sheet `ChuongTrinh`, `Data`, `ChiTieu`, `TraThuong`;
6. staged semantic upload vào SharePoint; workbook không đổi thì tránh ghi lại;
7. ghi audit `output/promotion_bonus_manifest.json` và state tại `06_BaoCaoTraThuong/_sync_state/promotion_bonus.json`.

Không chạy Promotion Bonus trong bootstrap/full-month rebuild/historical reconcile. Endpoint report không có ngày/as-of nên rebuild dữ liệu cũ sẽ tạo lịch sử giả. File `Current` luôn là snapshot mới nhất tại thời điểm sync.


## Concurrency và an toàn

Report sync, Data chấm ảnh, full-month rebuild, historical reconciliation và bootstrap dùng chung concurrency group `mobiwork-sharepoint-production` để tránh hai writer sửa SharePoint đồng thời.

Tất cả writer dùng `cancel-in-progress: false`. Recovery/rebuild phải **chờ** writer hiện tại hoàn tất; không cắt ngang một job đang publish vì điều đó có thể để một số report đã mới trong khi report khác vẫn cũ.

Bootstrap còn pause routine workflows sau khi nó lấy được production lock, và chỉ resume khi historical baseline hoàn tất thành công.

Excel được so sánh theo nội dung worksheet thay vì chỉ dựa vào kích thước hoặc raw-file hash.

## Production smoke

`production-smoke.yml` fetch lại MobiWork cho ngày mục tiêu và so dữ liệu source sau transform với đúng partition trong monthly master SharePoint.

Với mismatch có thể sửa, nó chạy một lần reconciliation có giới hạn (bounded one-shot recovery). Sau recovery, smoke chạy lại và workflow chỉ xanh khi consistency được xác nhận.

## Audit và giám sát

Report/rebuild/bootstrap/historical reconciliation ghi `output/sync_manifest.json`; Data chấm ảnh backfill ghi `output/data_cham_anh_backfill_manifest.json`; Promotion Bonus snapshot ghi `output/promotion_bonus_manifest.json`.

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

`operations-health.yml` kiểm độ mới của report sync, full-month rebuild (lần gần nhất phải `success`, có rebuild thành công trong 8 ngày) và production smoke. Khi lỗi kéo dài, nó mở/cập nhật issue `[OPS] MobiWork automation unhealthy` và tự đóng khi phục hồi.

Sau khi publish thành công, bước dọn file legacy xóa các file `__sync_tmp_*`, `__sync_backup_*`, `__sync_failed_*` còn sót trong cùng thư mục tháng (file bị xóa vẫn nằm trong Recycle Bin của SharePoint). Nếu log có `CRITICAL: unable to restore SharePoint backup`, lấy lại file từ backup hoặc Recycle Bin **trước** lần publish tiếp theo của tháng đó.

## Xử lý sự cố

1. Nếu đang bootstrap, xem Job Summary và `sync_manifest.json`; xác định `failed_month` và report lỗi.
2. Nếu Visit lỗi mapping, cập nhật `config/employee_regions.json` đúng prefix nhân viên rồi chạy lại; không fallback bằng `loai_kh`.
3. Nếu `new_customer` báo duplicate `makh`, không được loại record hoặc ép `makh` thành unique; kiểm `ID` của source vì `ID` mới là identity chuẩn.
4. Khi bootstrap chưa complete, giữ routine workflows ở trạng thái disabled.
5. Sau bootstrap, nếu dữ liệu một vài ngày sai/nhập trễ, chạy `lookback` phù hợp.
6. Nếu nghi một master tháng đã thiếu hoặc tích lũy sai, chạy `MobiWork Full Month Rebuild` cho tháng đó.
7. Nếu nghi chỉnh sửa cũ hơn tháng trước không được bắt, chạy `MobiWork Historical Reconciliation` từ tháng lịch sử cần kiểm tra.
8. Nếu API pagination báo repeated page hoặc total mismatch, không bỏ qua gate; kiểm source/API trước khi cho publish.
9. Nếu Data chấm ảnh thiếu/sai, chạy `MobiWork Full Month Rebuild` cho tháng đó (bước cuối tự làm mới Data chấm ảnh) hoặc chờ backfill thứ 2.
10. Phân biệt lỗi dữ liệu cố định với timeout/rate limit/API tạm thời trước khi retry nhiều lần.
11. `dry_run=true` không ghi SharePoint và không được dùng khi mục tiêu là sửa dữ liệu production.

## Sau thay đổi schema/code

Trước merge:

```text
compile -> Ruff -> unit tests -> coverage -> CI green
```

Nếu thay đổi schema monthly master hoặc mapping Vùng, production baseline nên được bootstrap/rebuild lại phạm vi tháng bị ảnh hưởng trước khi dashboard refresh.

Secrets bắt buộc: `MOBIWORK_USER`, `MOBIWORK_TOKEN`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`.

Repository variables tùy chọn (mặc định là production hiện tại): `SHAREPOINT_HOST`, `SHAREPOINT_SITE_PATH`, `SHAREPOINT_LIBRARY`.

Python lấy Graph token bằng GitHub OIDC assertion mới mỗi lần xin token (cần `permissions: id-token: write` và `AZURE_CLIENT_ID`/`AZURE_TENANT_ID` trong env), nên job dài không bị `AADSTS700024` khi assertion của `azure/login` hết hạn sau ~5 phút.

Data contract chi tiết: `docs/DATA_CONTRACT.md`.

### Promotion Bonus production dry-run gate (PR #91)

Before merging, open **Actions → MobiWork DMS Sync → Run workflow** and select
`feature/promotion-bonus-report`, `sync_scope=today`, `lookback_days=1`, and
`dry_run=true`. This uses real MobiWork secrets, fetches the complete catalogue and
one report per program, and builds the current workbook without creating a
SharePoint client or uploading a workbook/state JSON. Data chấm ảnh is skipped in
this dry-run as before.

Verify the **Promotion Bonus Snapshot** Job Summary: status, programs, data/target/
reward rows, workbook bytes, and dry-run flag. Download
`mobiwork-dry-run-output-<run_id>` for `BaoCaoTraThuong_Current.xlsx` (four sheets)
and `mobiwork-sync-manifests-<run_id>-<attempt>` for
`promotion_bonus_manifest.json`. An absent manifest is shown using the step
outcome, including skipped when an earlier stage fails.

Catalogue totals must remain stable and match both raw and unique program counts;
conflicting duplicates and invalid IDs fail before export. Archived/inactive
programs returned by the catalogue are included; no undocumented status filter is
applied. Missing/null optional target and reward arrays produce empty sheets.
Report provenance always comes from the requested catalogue program. Nested column
collisions and Excel cell/column limits fail rather than silently losing source
values. Strings beginning with `=` remain literal source data.

Local workbook replacement happens only after a complete staged build. SharePoint
uses the existing semantic comparison and verified staged replacement/rollback.
Promotion Bonus failure fails the workflow without undoing earlier report writes.
A state JSON upload failure also fails the local manifest/workflow; the already
verified workbook can have been published, and the next run retries state upload.
Retain PR #91 as Draft until the authenticated dry-run has passed. SharePoint/OIDC
permissions and live semantic no-op still require a non-dry-run production check.

## Báo cáo chi tiết CTKM theo khách hàng

`src/promotion_detail.py` chạy sau Promotion Bonus trong `MobiWork DMS Sync`.
Nguồn là `ChiTietSP` của monthly master Bill, không phải kết quả trả thưởng lũy kế.
Production đọc master từ SharePoint; dry-run đọc workbook local do report sync
vừa build (chỉ các ngày được yêu cầu, không tuyên bố đủ cả tháng).

Output local/artifact: `BaoCaoChiTietCTKM_YYYY-MM.xlsx`:
- `BaoCao`: 26 cột theo mẫu khách hàng cung cấp;
- `CanBoSung`: các trường/mapping còn thiếu, khóa đơn/dòng và ĐVT/sản lượng nguồn.

Quy tắc đã xác nhận:
- Mỗi dòng bán xuất một lần; cột J liệt kê các CTKM xuất hiện trên đơn, không
  khẳng định từng SKU được hưởng mọi CTKM đó.
- Dòng hàng tặng xuất riêng, cột X/Y/Z là sản phẩm/số lượng tặng thực tế. Không
  phân bổ tổng thưởng từ PromotionBonusReport vào đơn. Dòng tặng không lặp lại
  SELL-OUT và thành tiền của dòng bán; báo cáo không thực hiện Cartesian join.
- W là số lượng nguồn × `gia_truoc_vat` (trước VAT, chưa trừ chiết khấu). Không lấy
  giá sau VAT hoặc tự phân bổ chiết khấu cấp đơn. Thiếu đơn giá trước VAT là thiếu
  dữ liệu, không phải tiền hàng bằng 0.
- Ngày đơn dùng `ngay_dat` (fallback `ngay_ban_hang` nếu thiếu); nhân viên dùng người đặt (`ma_nv_dat`). Giữ nguyên
  trạng thái đơn của nguồn, chưa tự suy đoán mã trạng thái hủy/trả hàng.
- ĐVT Thùng/Két/Bình giữ nguyên. Chai hoặc ĐVT khác cần mapping SKU + ĐVT nguồn;
  chưa có hệ số thì để trống số lượng chuẩn và ghi issue, không ngầm dùng 24 chai.
- Giữ tên CTKM đầy đủ khi chưa có mapping mã. Không cắt tên ở dấu `_`, vì hậu tố
  như `_Q3` có ý nghĩa. Có thể ánh xạ tên/ID sang mã bằng `program_codes`.

Mapping ở `config/promotion_detail.json`: `employees` keyed by `ma_nv_dat`,
`customers` keyed by `ID_khachhang`, `products` keyed by `ma_sp`; giá trị là object
với tên cột đầu ra (Vùng/Tỉnh/SS Code/SS Name/DB Code/Tên NPP/Brand/Package/Loại KH).
`unit_conversions` keyed by `SKU|ĐVT nguồn`, value:
`{"target_unit": "Thùng", "factor": "0.04166666666666666666666666667"}`.
Ví dụ hệ số chỉ minh họa 1/24; phải thay bằng quy cách chính thức cho đúng SKU.
Không đưa credential hoặc dữ liệu khách hàng đầy đủ vào Git; chỉ dùng mapping
nghiệp vụ đã được phép lưu trữ trong config, hoặc cấu hình runtime riêng.

`publish_enabled=false` hiện tại: workflow tự tạo báo cáo và danh sách còn thiếu,
nhưng chưa ghi báo cáo này lên SharePoint. Sau khi mapping/định nghĩa dữ liệu được
xác nhận và dry-run không còn issue, đặt `publish_enabled=true`. Production sẽ
kiểm toàn bộ tháng chuẩn bị trước khi upload, từ chối publish nếu có issue. Folder:
`07_BaoCaoChiTietCTKM/YYYY/MM/`. Các upload dùng semantic no-op/staged verification
hiện có. Lỗi không rollback 4 report hay Promotion Bonus đã publish trước đó.
Audit: `output/promotion_detail_manifest.json`; Summary hiển thị `needs_mapping`
thay vì báo dữ liệu đã chuẩn khi thiếu thông tin. Đây là báo cáo phát sinh từ đơn,
không thay thế hoặc thêm history cho Promotion Bonus.

Có thể chọn config runtime riêng bằng `PROMOTION_DETAIL_CONFIG` để không đưa mapping khách hàng vào Git.

Mapping tham chiếu ban đầu đã trích từ file mẫu khách hàng gửi: 2 mã NVBH (Vùng/SS),
3 mã KH (Tỉnh/Loại KH/NPP, chỉ áp dụng khi NVBH cũng khớp), 4 SKU (Brand/Package).
`customer_codes` không coi mã KH là unique vô điều kiện: chỉ áp dụng khi source
có đúng một ID khách hàng cho mã đó và employee_code khớp. Không dùng NPP của một
khách hàng mẫu làm NPP mặc định cho toàn bộ nhân viên. Mapping này chỉ bao phủ các
mã trong mẫu, không tự mở rộng sang khách hàng/sản phẩm khác. Hai file không chứa
hệ số quy đổi Chai → Thùng/Két; `Package=1 way` không phải quy cách đóng gói.

Danh mục Product được lấy tự động qua
[findProduct](https://dms.mobiwork.vn/openapi/#/Product/findProduct) với phân trang,
kiểm total/repeated page/conflicting SKU; không đặt bộ lọc active hoặc ngày.
`nhan_hieu` bổ sung Brand khi config chưa có; `dvt_chan`, `dvt_le`, `hsqd` bổ sung
hệ số 1/hsqd từ ĐVT lẻ sang ĐVT chẵn khi ĐVT chẵn là Thùng/Két/Bình. Không thay
mapping đã xác nhận trong config. Không suy ra Package từ ngành hàng hoặc tên SKU.
Catalogue hiện tại không chứng minh quy cách lịch sử nếu một SKU đã đổi đóng gói;
đối với kỳ cũ cần xác nhận quy cách hoặc override phù hợp trong config. Unit test
không gọi API thật. `fetch_product_catalogue=true` dùng secrets MobiWork hiện có.

Nhận diện mã nghiệp vụ theo quy ước trong mẫu: `số/TB/GT/tháng/năm`, giữ hậu tố
`_Q1.._Q4` nếu có. Chỉ tách phần tên dài khi khớp quy ước này; tên/mã khác giữ
nguyên hoặc dùng `program_codes`, không cắt mọi chuỗi tại dấu `_`.
