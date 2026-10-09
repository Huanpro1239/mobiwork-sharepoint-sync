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

Trong mỗi lần `MobiWork DMS Sync`, sau khi luồng report chính và Data chấm ảnh hoàn tất,
`src/promotion_bonus.py` tính báo cáo tháng hiện tại (`auto` → `calc`):

```text
06_BaoCaoTraThuong/BaoCaoTraThuong_Current.xlsx
```

Luồng thực hiện:

1. phân trang `/OpenAPI/V1/PromotionBonus` với `page_size <= 200`;
2. lấy khách đăng ký/điều kiện từ chương trình và Bill `ChiTietSP` của tháng;
3. bỏ hàng khuyến mãi, lọc ngày giao theo Việt Nam, chỉ cộng SKU/ĐVT khai trong chương trình;
4. nối danh mục khách và DisplayData; chỉ `sttt=0` hoặc mặc định được hỗ trợ;
5. tạo `BaoCao`, `Tong_hop`, `Ket_qua`, `Kiem_tra` và sheet lỗi khi cần;
6. staged semantic upload vào SharePoint; workbook không đổi thì tránh ghi lại;
7. ghi audit `output/promotion_bonus_manifest.json` và state tại `06_BaoCaoTraThuong/_sync_state/promotion_bonus.json`.

`promotion_history` hoặc `bonus_months` tính lại tháng từ Bill monthly master, lưu
`06_BaoCaoTraThuong/YYYY/MM/BaoCaoTraThuong_YYYY-MM.xlsx`. Backfill không ghi đè Current.
Khách đăng ký/cây phòng ban dùng danh mục hiện tại; lịch sử và danh sách khách DMS
chưa được xác nhận tương đương hoàn toàn.

`Thưởng dự kiến` giữ quà của khách đạt doanh số; `Ket_qua` và dòng `TRẢ THƯỞNG` chỉ
gồm khách đủ điều kiện, kể cả kết quả trưng bày khi chương trình yêu cầu.
Khu vực khách lấy từ metadata hoặc vùng duy nhất của nhân viên trên đơn bán;
`Vùng áp dụng CT` được ghi riêng. Nhiều vùng/NPP thì để trống, không chọn tùy ý.

Chương trình "Mua nhiều sản phẩm - đạt số tiền - tặng tiền" (`MUTI_SP_ST_TIEN`, vd 246/248 Đảnh
Thạnh mùa hè): quà là tiền/voucher trong `khuyen_mai` (đồng); dòng TRẢ THƯỞNG ghi `TIEN` / Tiền
thưởng, số lượng = số tiền. Mã CTKM giữ hậu tố `- Loại A/B/C/D`.

Chương trình tích lũy cả kỳ (không theo tháng) khai trong `config/promotion_bonus.json`
`cumulative_programs` (tiền tố mã, vd `"246/TB/GT/04/2026"`). Báo cáo tháng tính lũy kế từ ngày bắt
đầu CT đến hết tháng báo cáo; trước tháng kết thúc ghi "Tạm tính – CT kết thúc dd/mm/yyyy", chưa có
dòng TRẢ THƯỞNG. Tháng không có file đơn hàng được ghi trong Kiem_tra (DMS không có đơn trước 06/2026).
Chương trình Q4 "... THEO THÁNG" vẫn tính theo từng tháng.

### Chạy tự động (không cần thao tác)

- Lịch `mobiwork-sync`: 3 giờ/lần trong giờ làm việc (T2–T7) + 16:00 hằng ngày. Mỗi lần: đồng bộ đơn
  hàng → báo cáo trả thưởng tháng hiện tại (`BaoCaoTraThuong_Current.xlsx` + file tháng) → báo cáo chi
  tiết CTKM → cập nhật `08_BoSungDanhMuc/CanBoSung_TongHop.xlsx`.
- Ngày 1–7 hằng tháng (`PROMOTION_BONUS_PREVIOUS_DAYS` / `PROMOTION_DETAIL_PREVIOUS_DAYS` = 7): tính lại
  cả tháng trước (đơn giao muộn, sửa lùi ngày, rebuild master ngày 2, CT tích lũy kết thúc tháng trước).
- Chương trình mới trên DMS tự vào báo cáo. Cần người dùng xác nhận trong sheet `ChuongTrinh`:
  - CT kéo dài nhiều tháng mà tên không có "THEO THÁNG": điền `Cách tính` = "Tích lũy cả kỳ" hoặc
    "Theo tháng" vào `BoSung_Mapping.xlsx` (khóa = `Mã CT` dạng `246/TB/GT/04/2026`). Chưa khai thì giữ
    phần thưởng dự kiến, chưa xác nhận trả thưởng.
  - CT "Không tính được" (loại quy tắc chưa hỗ trợ) và "Vượt số suất" (đủ điều kiện > `soSuat`).
- Annotation `Chương trình cần xem MM/YYYY` báo số CT cần xử lý ở mỗi lần chạy.
- File `BoSung_Mapping.xlsx` cũ được tự thêm sheet mới (giữ nguyên dữ liệu đã điền).

`CanBoSung.Mức độ` phân biệt `Thiếu thông tin mô tả`, `Thiếu quy đổi đơn vị` và `Chặn xuất bản`.
Thiếu quy cách giữ số lượng quy đổi trống và báo `unit_gaps`, theo cùng chính sách CTKM.
Lỗi identity, số tiền/số lượng hoặc hệ số quy đổi không hợp lệ chặn upload; dùng dry-run để xem chi tiết.
Lỗi dòng bán đủ điều kiện nhưng thiếu ĐVT ghi tổng số dòng thiếu và tối đa 25 khóa
đơn/dòng/SKU, số lượng, mã CT trong manifest lỗi. Không ghi tên, địa chỉ, điện thoại KH;
không trả kết quả tính một phần. `line_unit_overrides` cần xác nhận ĐVT đúng dòng;
`sale_unit_defaults` cần xác nhận mặc định cho SKU khi API trống ĐVT. ĐVT nguồn được
ưu tiên, sau đó khai báo đúng dòng, rồi mặc định SKU; không dùng mặc định dòng bán cho quà.
Manifest có `blocking_issues`, `missing_fields`, `quality_status` và số khách cần
kiểm tra trưng bày. `status=success` là chạy xong, không đồng nghĩa đã đối chiếu DMS.

API thay đổi tổng khi phân trang được đọc lại cả khoảng tối đa 3 lần (giới hạn thêm
bởi `MOBIWORK_MAX_RETRIES`; 0 là không thử lại). Mọi lần đọc có bộ nhớ riêng;
vẫn từ chối dữ liệu nếu tổng không ổn định hoặc identity bị xung đột.


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
- ĐVT Thùng/Két/Bình giữ nguyên; hàng tính theo Cái (vật phẩm) không có quy cách thùng
  giữ nguyên theo CÁI. Chai hoặc ĐVT khác cần mapping SKU + ĐVT nguồn; chưa có hệ số
  (hoặc đơn hàng thiếu ĐVT) thì để trống số lượng chuẩn và ghi issue "Thiếu quy đổi…" —
  issue này KHÔNG chặn xuất báo cáo (manifest `unit_gaps`), không ngầm dùng 24 chai.
- Giữ tên CTKM đầy đủ khi chưa có mapping mã. Không cắt tên ở dấu `_`, vì hậu tố
  như `_Q3` có ý nghĩa. Có thể ánh xạ tên/ID sang mã bằng `program_codes`.

Mapping ở `config/promotion_detail.json`: `employees` keyed by `ma_nv_dat`,
`customers` keyed by `ID_khachhang`, `products` keyed by `ma_sp`; giá trị là object
với tên cột đầu ra (Vùng/Tỉnh/SS Code/SS Name/DB Code/Tên NPP/Brand/Package/Loại KH).
`fetch_sales_structure=true` tự suy ra mapping nhân viên từ cây phòng ban DMS
(OpenAPI `Sale` + `SaleGroup`): DB Code/Tên NPP = đơn vị trực tiếp dạng `B-XXXX-NNNN`
của nhân viên; SS Code/SS Name = nhân viên có chức vụ "Giám sát kinh doanh" (hoặc chức
danh "Giám sát" khi chức vụ trống) cùng đơn vị — `ma_don_vi` có thể chứa nhiều đơn vị cách
nhau dấu phẩy — hoặc đơn vị cha gần nhất; NPP chưa gán giám sát dùng giám sát duy nhất
của tỉnh (B-<TỈNH>-NNNN), tỉnh có nhiều giám sát thì để trống để bổ sung trên DMS; Vùng = đơn vị cha mã MB/MT/MN/TN (vd `MT1B` → "Miền Trung 1B").
Mapping khai báo tay trong `employees` luôn được ưu tiên. Áp dụng cho cả báo cáo trả thưởng.
`unit_conversions` keyed by `SKU|ĐVT nguồn`, value:
`{"target_unit": "Thùng", "factor": "0.04166666666666666666666666667"}`.
Ví dụ hệ số chỉ minh họa 1/24; phải thay bằng quy cách chính thức cho đúng SKU.
Không đưa credential hoặc dữ liệu khách hàng đầy đủ vào Git; chỉ dùng mapping
nghiệp vụ đã được phép lưu trữ trong config, hoặc cấu hình runtime riêng.

`publish_enabled=true` hiện tại theo yêu cầu người dùng: báo cáo chạy và xuất
cùng luồng đơn hàng/chấm ảnh. `allow_incomplete_publish=true` cho phép giữ cảnh
báo thiếu metadata trong `CanBoSung`; lỗi số lượng, giá, ngày và khóa khách hàng
vẫn chặn xuất. Production kiểm toàn bộ tháng chuẩn bị trước khi upload. Folder:
`07_BaoCaoChiTietCTKM/YYYY/MM/`. Các upload dùng semantic no-op/staged verification
hiện có. Lỗi không rollback 4 report hay Promotion Bonus đã publish trước đó.
Audit: `output/promotion_detail_manifest.json`; Summary hiển thị `needs_mapping`
thay vì báo dữ liệu đã chuẩn khi thiếu thông tin. Đây là báo cáo phát sinh từ đơn,
không thay thế hoặc thêm history cho Promotion Bonus.

### Bổ sung CanBoSung (NPP theo kho xuất + file BoSung_Mapping)

- NPP của mỗi dòng = mã kho xuất trên đơn (`ma_kho_xuat`, hàng tặng `ma_kho_xuat_km` bỏ tiền tố
  `KM - `), dạng `B-XXXX-NNNN`. Đây là NPP tại thời điểm bán nên đúng cả cho tháng cũ khi nhân
  viên đã nghỉ/chuyển. Tên NPP / SS / Vùng lấy theo đơn vị đó trên cây phòng ban (`npp_units`).
  Đơn không có kho xuất mới dùng đơn vị hiện tại của nhân viên.
- Thứ tự ưu tiên: `BoSung_Mapping.xlsx` > `employees` trong config > kho xuất của đơn >
  cây phòng ban hiện tại > danh mục DMS. Ô trống không ghi đè.
- Mỗi lần chạy, `08_BoSungDanhMuc/CanBoSung_TongHop.xlsx` liệt kê mỗi NPP / nhân viên / sản phẩm /
  ĐVT / khách hàng còn thiếu đúng 1 dòng (cột `Còn thiếu`, `Hiện có`, `Gợi ý`, `Số dòng`, `Nguồn`),
  gộp mọi tháng đã chạy (state `08_BoSungDanhMuc/_sync_state/canbosung_todo.json`). Sheet
  `SuaTrenDMS` là lỗi dữ liệu gốc phải sửa trên DMS.
- Người dùng điền cột vàng trong `08_BoSungDanhMuc/BoSung_Mapping.xlsx` (pipeline tạo một lần,
  không bao giờ ghi đè). QuyDoi nhập `ĐVT báo cáo` (Thùng/Két/Bình) và số ĐVT nguồn trong 1 ĐVT
  báo cáo (vd 24). Dòng sai bị bỏ qua và báo trong annotation `BoSung_Mapping`.
- Lần đồng bộ sau tự áp dụng; tháng cũ chạy `report_scope=promotion_history`.
- Khách hàng đổi mã trên DMS nhưng cùng ID: đơn giữ mã cũ, metadata lấy theo ID, không chặn.
- Tắt: `"bosung_mapping": false` trong `config/promotion_detail.json`; bước trả thưởng cần thêm
  env `BOSUNG_MAPPING=true` (đã đặt trong workflow).

Có thể chọn config runtime riêng bằng `PROMOTION_DETAIL_CONFIG` để không đưa mapping khách hàng vào Git.

File mẫu chỉ quy định cơ cấu 26 cột và định dạng, không cung cấp mapping dữ liệu.
Đã bỏ mapping Vùng/SS/NPP/Loại KH trích từ các dòng mẫu. Báo cáo không lọc theo
miền, nhân viên hoặc khách hàng mẫu. Vùng dùng chung `config/employee_regions.json`
theo prefix `ma_nv_dat`, bao gồm Bắc/Trung/Nam; mã chưa biết giữ trống và ghi issue.
SS/NPP chưa có nguồn xác nhận giữ trống; Tỉnh/Loại KH lấy từ DMS theo ID khách hàng.
Danh mục sản phẩm người dùng cung cấp vẫn là nguồn Brand/Package/quy đổi chính thức.

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

Vật phẩm tặng thực tế như SKU 530200025 (ly) có ĐVT `Cái`: giữ số lượng Cái,
không quy đổi ly sang Thùng/Két và không coi là lỗi thiếu quy đổi. Sheet `DonViTinh`
ghi khóa đơn/dòng, SKU, ĐVT nguồn/đích, hệ số và dấu hiệu hàng tặng để kiểm tra.
Không cộng hàng tặng khác đơn vị vào một tổng số lượng chung. Gate metadata vẫn
áp dụng cho các trường mẫu còn thiếu.

Danh mục quy đổi đã nhập từ `Danh Muc San Pham.xlsx`, sheet `DanhMucSanPham`:
hai cột `MaSanPhamMoi_Vikoda`/`MaSanPhamMoi_VKD` cùng ánh xạ một sản phẩm;
`Brand` → Brand, `Group` → Package, `Quy cách` và `ĐVT` → hệ số 1/quy cách.
132 dòng có sản phẩm cung cấp 264 mã; hai mã bán thành phẩm không có quy cách
chỉ bổ sung metadata. Cấu hình lưu tên file, SHA-256 và dòng nguồn để đối chiếu.
Không đưa workbook nguồn vào Git. Danh mục này ưu tiên hơn dữ liệu Product API;
API vẫn bổ sung các SKU chưa có trong file. Vật phẩm ly ngoài danh mục vẫn cần
Package phù hợp nếu nghiệp vụ yêu cầu trường này.

Cập nhật khi nhận phiên bản danh mục mới, từ thư mục repository:

```powershell
$env:PYTHONPATH = 'src'
python -m import_product_reference 'D:\Vikoda\SO\Bao cao mau\DMSP\Danh Muc San Pham.xlsx'
```

Importer kiểm tra mã trùng mâu thuẫn, quy cách nguyên dương và đơn vị đóng gói;
nếu lỗi thì giữ nguyên config. Khi thành công, thay hệ số cũ của các mã có trong
file, giữ mapping nhân viên/khách hàng và các SKU ngoài file. ĐVT Chai dùng theo
Bill đã kiểm tra; sản phẩm có `Brand 1=Lon` bổ sung cả ĐVT Lon. Tiền trước VAT
vẫn tính bằng số lượng gốc, không nhân đơn giá với số thùng đã quy đổi.

Customer catalogue dùng GET `/OpenAPI/V1/Customer`, độc lập với report khách hàng
mở mới theo ngày. `fetch_customer_catalogue=true` tải phân trang theo ngày tạo
từ `customer_catalogue_start_date` (01/01/1900) đến ngày hiện tại Việt Nam, không
lọc active/nhân viên/phòng ban. Total, trang lặp và ID trùng được kiểm tra trước
khi tạo report; không dừng ở trang ngắn nếu API không trả total.

Nối `Customer.ID` với `Bill.ID_khachhang`, đồng thời kiểm tra `makh` nếu có;
không nối theo tên hay mã KH đơn lẻ. `tinh_thanh_moi` bổ sung Tỉnh, `loai_kh`
bổ sung Loại KH và `code_router` dạng chuỗi bổ sung Route. Trường danh sách/object
được đếm ở `structured_fields_not_mapped`, không tự chọn tuyến của đơn từ danh
sách tuyến khách hàng hiện tại. Tên/địa chỉ/điện thoại và các
giá trị trên Bill chỉ bổ sung khi trống; mapping xác nhận trong config ưu tiên.
Không suy SS/NPP từ `nv_pt`, `nhom_kh`, `kenh` hay địa chỉ. Catalogue là dữ liệu
hiện tại, chưa chứng minh phân loại khách hàng tại thời điểm đơn lịch sử.

Metadata khách hàng chỉ tồn tại trong bộ nhớ, không ghi vào config Git. Manifest
lưu số lượng, tên trường, phạm vi ngày và khóa nối, không lưu tên/địa chỉ/điện thoại.
Các giá trị này vẫn có trong workbook báo cáo theo mẫu. Thiếu ID hoặc không khớp
được khách hàng vẫn ghi nhận các trường còn thiếu, không bịa giá trị thay thế.

CTKM dùng cùng workflow, lịch chạy, MobiWork secrets, Azure app, SharePoint drive
và cấu hình retry với đơn hàng/chấm ảnh; không cần app hoặc secret riêng. Đã bỏ
hai input xuất CTKM riêng. `dry_run=false` xuất tất cả báo cáo bật trong config;
`dry_run=true` không ghi SharePoint. Workbook giữ `CanBoSung`; manifest ghi
`published_with_issues`, số issue, đường dẫn remote và `workbook_published` khi
xuất bản với metadata thiếu theo yêu cầu đã xác nhận. Đặt
`allow_incomplete_publish=false` để khôi phục gate mọi metadata phải đầy đủ.

Nhánh thử nghiệm vẫn cần OIDC subject được Entra tin cậy; dùng chung Azure app
không làm token của nhánh thử nghiệm thành token nhánh production. Chưa merge
PR thì lịch main chưa có các bước mới. Không đổi subject GitHub hay chạy mã nhánh
thử nghiệm dưới token main để bỏ qua kiểm tra tin cậy.

Xuất lịch sử CTKM theo mẫu: chạy cùng workflow `MobiWork DMS Sync`, chọn
`report_scope=promotion_history`. Chế độ này đọc các folder năm/tháng có sẵn
trong `04_DonBanHang`, kiểm tra workbook master chuẩn, rồi tạo mỗi tháng một
file. Không refetch hoặc ghi đè các report đơn hàng/ảnh, không gán snapshot
PromotionBonus hiện tại thành lịch sử. Dry-run lịch sử chỉ đọc SharePoint và
tạo file local; không upload. Tháng tương lai bị loại; folder tháng thiếu master
là lỗi, không tự tạo báo cáo rỗng. Giới hạn 120 tháng.

Để xuất lại riêng một tháng, chọn `report_scope=promotion_history` và nhập
`bonus_months=2026-09` (hoặc danh sách tháng `YYYY-MM` cách nhau bằng dấu phẩy).
Cả trả thưởng và chi tiết CTKM chỉ đọc master của các tháng đã chọn và ghi file
vào thư mục tháng tương ứng; không cập nhật `BaoCaoTraThuong_Current.xlsx`.
Để trống `bonus_months` giữ chế độ lịch sử toàn bộ. Tháng tương lai hoặc chuỗi
tháng không hợp lệ bị chặn trước khi đọc/ghi báo cáo.

Ngày `ngay_dat` (fallback `ngay_ban_hang`) xác định tháng báo cáo. Dòng thuộc
tháng khác được đếm trong `outside_order_month_rows`; ngày lỗi vẫn giữ để gate
chặn xuất. Manifest ghi source month, số dòng nguồn và SHA-256 workbook nguồn.
Sheet BaoCao dùng header tại dòng 4, 26 cột, tiêu đề, màu header và độ rộng cột
trích từ file mẫu, kèm tháng báo cáo. Không lưu dữ liệu mẫu khách hàng vào Git.

Đối chiếu Paybonus chưa được khẳng định: API hiện trả Data rỗng; danh mục có
cả chương trình đã khóa nhưng chưa tìm thấy mã Q3 cụ thể trong mẫu. Các dòng
hiện tại dựa trên liên kết promotion/CTKM trên Bill; đơn tích lũy không có liên
kết đó chưa được tự suy là thuộc chương trình. Cần đối chiếu phiên Paybonus web
đã đăng nhập trước khi xác nhận độ phủ của chương trình trả thưởng và các đơn
tích lũy. Không phân bổ tổng thưởng khách hàng vào từng đơn hoặc gọi thưởng đạt
được là hàng tặng thực tế.

### Tên và mã CTKM dễ đọc trong báo cáo chi tiết

Đầu ra `BaoCao` giữ đúng 26 cột theo cơ cấu mẫu. `fetch_program_catalogue=true`
tra danh mục PromotionBonus để đổi ID nội bộ sang mã nghiệp vụ trong tên chương
trình, hoặc giữ tên DMS khi không có mã theo quy ước. Áp dụng cho promotion.id,
ctkmFull_id ở cả dòng bán và hàng tặng; không thêm cột kỹ thuật vào BaoCao.
Mã khách hàng/nhân viên/sản phẩm vẫn là mã nghiệp vụ nguồn, với tên ở cột riêng.
Không dùng dòng mẫu làm dữ liệu danh mục hoặc tự đặt tên khi ID chưa tra được.
## Xử lý phần còn thiếu ngày 08/10/2026

SS Code/SS Name được phép trống theo yêu cầu của người dùng; không chặn tính thưởng
và không còn là mục phải bổ sung trong CanBoSung_TongHop, kể cả ở trạng thái tháng cũ.
Cấu hình `allow_blank_fields` chỉ chấp nhận trường mô tả, không cho phép bỏ qua lỗi
đơn hàng, số lượng, giá, ngày hoặc định danh.

Các bổ sung có nguồn cụ thể:

- Người dùng xác nhận tính **theo tháng, không cộng dồn** cho 18 mã CT hiện còn
  thiếu cách tính: 010/011/013 (Q4), 012, 558–563, 570–572, 575, 581, 584, 588,
  589. `program_overrides` khai rõ từng mã; không suy cách tính cho CT mới khác.
- Ngày 09/10/2026, người dùng xác nhận mặc định `Chai` khi API trống ĐVT cho SKU
  `230100011` và `230100017`. `sale_unit_defaults` thay ba khai báo dòng trùng của
  hai SKU này, áp dụng cả đơn mới. `DonViTinh` ghi nguồn xác nhận; ĐVT gốc được ưu tiên.
- Hai dòng quà cùng đơn có thể phục hồi ĐVT từ `promotion.product.don_vi_tinh` trên
  chính đơn, khi CTKM ID và SKU khớp và chỉ có một đơn vị. Không suy ĐVT dòng bán
  từ đơn vị quà hoặc giá, không lấy mặc định theo danh mục hiện tại.
- Package của `530200025` là `Vật phẩm`, được người dùng xác nhận.
- Tỉnh ưu tiên `tinh_thanh_moi`, rồi `tinhthanh_pho`; nếu cả hai trống thì khớp
  chính xác địa danh cuối địa chỉ DMS với `customer_address_provinces`. Các địa danh
  `Lâm Đồng`/`Phú Yên` được thấy trong địa chỉ của 7 khách còn thiếu. Đây là địa danh
  nguồn, không phải quy đổi sang địa giới mới hoặc suy từ mã NV/KH. Manifest ghi
  số lượng theo nguồn fallback; `Kiem_tra` giải thích cách dùng.

Chương trình nhiều tháng chưa xác định cách tính vẫn hiển thị khách, doanh số trong
kỳ báo cáo và thưởng dự kiến. Chưa xác nhận thưởng cho đến khi khai `Theo tháng`
hoặc `Tích lũy cả kỳ` trong BoSung_Mapping. `Can_xem` chỉ rõ CT cần xử lý ngay trong
file trả thưởng; manifest không coi đó là đầy đủ dù không thiếu trường mẫu.
Chương trình tích lũy được giữ tạm tính cả ở tháng đầu; thiếu một tháng DonBanHang
thì chưa xác nhận thưởng, kể cả tháng kết thúc. Một master tháng có nội dung rỗng
được coi là không có đơn; không tìm thấy master là thiếu dữ liệu, không phải số 0.

## Lỗi không còn dừng cả báo cáo trả thưởng
- Dòng đơn thuộc CT nhưng thiếu ĐVT: chỉ khách × CT liên quan bị tạm giữ ("Chờ xác nhận ĐVT dòng đơn", không trả thưởng);
  phần còn lại vẫn xuất bản. Danh sách đơn/dòng/SP ở annotation "Dòng đơn thiếu ĐVT" và sheet ChuongTrinh của CanBoSung_TongHop.
- `BaoCaoTraThuong_Current.xlsx` đang mở trong Excel (423): file tháng vẫn được ghi, run báo warning; Current cập nhật ở lần sau.
- Kết quả trưng bày (`DisplayData.cham_diem`) được đọc ở mọi dạng (dict, list, chuỗi JSON); annotation DisplayData ghi
  `grading_shape` và `status_by_grading` để đối chiếu `tt_cham_diem`.

## Trưng bày và số suất (09/10/2026)
- DisplayData được lấy thêm theo từng chương trình trưng bày (`ten_cttb`, tham số trong tài liệu OpenAPI) trên cả
  thời gian CT; "Đạt" chỉ khi `cham_diem` ghi đúng "Đạt" (tài liệu: `tt_cham_diem = 2` vẫn có bản ghi chưa có kết quả,
  nên trạng thái này không được hiểu là Đạt). Annotation DisplayData có `by_programme` (số bản ghi, số bản ghi có kết quả).
- `soSuat` = tổng số suất của CT (mỗi bội số 1 suất), `gioiHanCT = false`/trống = không giới hạn. Suất chia theo ngày
  khách đạt chỉ tiêu (sớm trước, rồi mã KH); CT theo tháng trừ suất đã trả ở các tháng trước của CT. Khách đạt sau khi
  hết suất: "Hết suất CT (khách đạt sau)", không trả. Chi tiết theo CT ở manifest `quota` và sheet ChuongTrinh.
- Kết quả trưng bày: DMS DisplayData không trả kết quả chấm (`cham_diem` rỗng ở mọi bản ghi, kể cả khi lọc theo
  `ten_cttb`). Khách đạt doanh số nhưng chờ trưng bày được liệt kê ở sheet **TrungBay** của CanBoSung_TongHop; điền
  `Kết quả` = Đạt / Không đạt vào sheet TrungBay của BoSung_Mapping (khóa Mã KH + tên CT trưng bày). Đạt → trả thưởng
  (nếu còn suất); Không đạt → "Không đạt trưng bày", không trả.
