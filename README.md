# MobiWork DMS → SharePoint

Production data pipeline bằng Python để nạp 4 báo cáo lịch sử từ MobiWork DMS Open API (viếng thăm, mở mới khách hàng, đơn đặt hàng, đơn bán hàng) vào thư viện SharePoint `MobiWorkDMS`, kèm workbook **Data chấm ảnh** hằng tháng và snapshot **Báo cáo trả thưởng** hiện hành.

```text
MobiWork Open API
        │
        ├─ Report fetch + validation
        │        │
        │        ├─ pagination completeness / repeated-page guard
        │        ├─ exact-overlap dedupe / conflict guard
        │        ├─ business-key validation
        │        ├─ employee-region enrichment cho Visit
        │        └─ monthly merge / full-month rebuild
        │                 │
        │                 ├─ partition quality gate
        │                 ├─ report-month atomic publish gate
        │                 ▼
        │          Semantic SharePoint publish
        │                 │
        │                 ▼
        │          Monthly master Excel
        │
        └─ Data chấm ảnh (từ monthly master Viếng thăm + Đơn bán hàng)
                  │
                  ▼
          05_DataChamAnh/YYYY/MM/Data_cham_anh_YYYY-MM.xlsx
            ├─ Data_anh: mỗi link ảnh viếng thăm một dòng (link gốc MobiWork)
            └─ Data_don_hang: chi tiết đơn bán hàng
```

Dự án chỉ tạo **nguồn dữ liệu chuẩn**. Nó không tải/copy file ảnh lên SharePoint (Data chấm ảnh dùng link ảnh gốc của MobiWork), không chấm điểm ảnh và không tạo KPI nghiệp vụ.

## Bootstrap production trước khi chạy lịch

Production mới hoặc production vừa thay đổi logic dữ liệu phải chạy **`MobiWork Bootstrap Full History`** trước khi để automation định kỳ tiếp tục.

Bootstrap mặc định chạy từ `2026-06` vì đây là tháng lịch sử sớm nhất hiện đang tồn tại trong SharePoint production. Có thể nhập tháng sớm hơn nếu MobiWork thực tế có dữ liệu cũ hơn.

Luồng bootstrap:

```text
2026-06
  ↓ full rebuild 4 report
2026-07
  ↓ full rebuild 4 report
2026-08
  ↓ full rebuild 4 report
tháng hiện tại
  ↓ rebuild đến ngày hiện tại
  ↓
bootstrap_complete = true
  ↓
resume hourly/nightly/weekly/monthly automation
```

Một bootstrap production (`dry_run=false`) sẽ:

- giữ production writer lock trong toàn bộ lần chạy;
- chờ writer đang chạy hoàn tất, không cancel job đang ghi SharePoint;
- tạm **disable** các workflow routine trước khi rebuild;
- **không disable `MobiWork Full Month Rebuild`**, để vẫn còn recovery tool nếu bootstrap fail;
- rebuild từng tháng theo thứ tự cũ → mới;
- mỗi tháng phải pass source completeness gate cho toàn bộ report;
- dừng ngay trước các tháng sau nếu có một tháng lỗi;
- ghi trạng thái vào `_sync_state/bootstrap.json`;
- chỉ **enable lại** routine workflows sau khi tất cả tháng hoàn tất thành công.

Nếu bootstrap fail hoặc bị cancel, routine automation vẫn bị pause để không cập nhật tiếp trên một historical baseline chưa đầy đủ. Manual full-month rebuild vẫn có thể chạy và bypass bootstrap readiness gate để phục hồi một tháng riêng lẻ. Sau khi sửa nguyên nhân, chạy lại bootstrap từ tháng chưa hoàn tất hoặc từ đầu range cần xác nhận.

## Data model production

Các report được khai báo tại `config/reports.json`:

| Key | Workbook | Kiểu | Business key / upsert |
|---|---|---|---|
| `visit` | `BaoCaoViengTham_YYYY-MM.xlsx` | flat | thay partition theo ngày; không cross-day upsert |
| `new_customer` | `MoMoiKhachHang_YYYY-MM.xlsx` | flat | `ID` của record MobiWork |
| `order` | `DonDatHang_YYYY-MM.xlsx` | header + detail | `ma_phieu` |
| `bill` | `DonBanHang_YYYY-MM.xlsx` | header + detail | `ma_phieu` |

Báo cáo trả thưởng là dataset snapshot riêng, cấu hình tại `config/promotion_bonus.json`, vì endpoint `PromotionBonusReport` bắt buộc `id_ct` và không hỗ trợ truy vấn trạng thái lịch sử theo ngày:

```text
06_BaoCaoTraThuong/BaoCaoTraThuong_Current.xlsx
├─ ChuongTrinh
├─ Data
├─ ChiTieu
└─ TraThuong
```

Tài liệu API chính thức: [Danh sách chương trình trả thưởng](https://dms.mobiwork.vn/openapi/#/PromotionBonus/findPromotionBonus) và [Báo cáo trả thưởng](https://dms.mobiwork.vn/openapi/#/PromotionBonusReport/findPromotionBonusReport). Chi tiết tham số và mapping nằm trong [data contract](docs/DATA_CONTRACT.md#promotion-bonus-snapshot-contract).

Pipeline tự phân trang `/OpenAPI/V1/PromotionBonus` để lấy toàn bộ chương trình, sau đó gọi `/OpenAPI/V1/PromotionBonusReport` từng chương trình để giữ provenance chính xác. Snapshot được refresh trong workflow `MobiWork DMS Sync`; bootstrap/full-month rebuild 4 report lịch sử không giả lập backfill cho dataset này.

`makh` của `new_customer` là mã nghiệp vụ và **không được giả định unique**: dữ liệu lịch sử đã có các record khác `ID` nhưng dùng lại cùng `makh`. Pipeline giữ đủ các record đó và dùng `ID` làm identity/upsert key. `order` và `bill` kiểm uniqueness header theo `ma_phieu`; detail kiểm theo `ma_phieu + stt`. `bill` còn đối chiếu `API total == fetched rows` trước khi chấp nhận dữ liệu.

### Quy tắc Vùng cho báo cáo viếng thăm

`loai_kh` là **phân loại khách hàng**, không phải Vùng bán hàng. Pipeline gắn thêm:

- `vung_code`
- `vung`
- `vung_source`

Vùng được xác định từ `ma_nv` theo `config/employee_regions.json`. Production **không làm mất cả report** khi xuất hiện prefix nhân viên mới chưa được mapping. Record vẫn được giữ với:

```text
vung_code   = UNMAPPED
vung        = Chưa phân vùng
vung_source = unmapped
```

Log sẽ cảnh báo để bổ sung mapping sau. Strict mode vẫn tồn tại cho test/validation khi cần. Không dùng `loai_kh` làm fallback vì sẽ phân sai Vùng.

> Dashboard/Power BI phải dùng cột `vung` để lọc Vùng. Nên hiển thị riêng `Chưa phân vùng` để dễ phát hiện mã nhân viên mới cần mapping.

Chi tiết xem [`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md).

## Cơ chế bảo vệ dữ liệu

- Paginated API không còn coi một page ngắn hơn `page_size` là EOF. Nếu source không có `total`, pipeline tiếp tục cho đến page rỗng; nếu API lặp lại cùng page, job fail thay vì ghi dữ liệu trùng/thiếu.
- Nếu hai page chồng biên và trả **record hoàn toàn giống nhau** với cùng primary key, pipeline collapse exact duplicate an toàn. Nếu cùng business key nhưng payload khác nhau, job vẫn fail để không tự đoán version nào đúng.
- Một workbook canonical cho mỗi report/tháng; `_sync_date` lưu partition ngày và được ẩn trong Excel.
- Upsert cross-day dùng `upsert_keys` khai báo rõ trong `reports.json`, không suy luận từ tên cột.
- Sau mỗi merge có **partition quality gate**: dữ liệu vừa fetch phải hiện diện đầy đủ trong partition kết quả; `order`/`bill` còn kiểm không để lại detail cũ của cùng `ma_phieu`.
- Incremental/lookback có **report-month atomic publish gate**: nếu một target date trong cùng report/tháng fail thì workbook partial không được publish; canonical SharePoint cũ giữ nguyên để retry sau.
- Staged SharePoint upload → semantic verification → promote → rollback/backup khi cần. Mỗi lần ghi tạo **driveItem mới** (item ID đổi, lịch sử phiên bản SharePoint không nối tiếp); tham chiếu file theo **đường dẫn**, không theo item ID hoặc link chia sẻ.
- Không ghi lại workbook nếu nội dung nghiệp vụ không đổi.
- Full-month rebuild không đọc master cũ; fetch lại toàn bộ ngày từ MobiWork.
- Full-month rebuild có **global source gate**: tất cả report/tất cả ngày phải build thành công trước lần ghi SharePoint đầu tiên.
- Nếu publish SharePoint lỗi, các report phía sau bị chặn để giảm trạng thái nửa cũ/nửa mới.
- Production smoke fetch lại MobiWork và so trực tiếp partition SharePoint với source mới.
- Operations health mở GitHub issue khi automation production mất freshness/consistency.

## Tự động hóa

- `.github/workflows/mobiwork-bootstrap-history.yml`
  - one-time historical bootstrap trước khi production schedule tiếp tục.
Tất cả lịch dùng múi giờ `Asia/Ho_Chi_Minh` (khóa `timezone:` trong `schedule`). GitHub có thể chạy lịch trễ vài chục phút đến vài giờ khi tải cao.

- `.github/workflows/mobiwork-sync.yml`
  - `07:05, 10:05, 13:05, 16:05, 19:05` thứ 2–thứ 7: refresh `today`.
  - `09:00` hằng ngày: refresh `yesterday` (chốt ngày hôm qua) và rebuild Data chấm ảnh.
- `.github/workflows/nightly-reconcile.yml`
  - `23:30`: queue `mobiwork-sync` lookback **3 ngày đã hoàn tất** (D-1..D-3).
- `.github/workflows/recovery-rebuild.yml`
  - Chủ nhật `02:00`: full rebuild tháng hiện tại.
  - Ngày 2 mỗi tháng `03:30`: full rebuild tháng trước để khóa sổ.
- `.github/workflows/data-cham-anh-backfill.yml`: thứ 2 `10:15`, backfill workbook Data chấm ảnh.
- `.github/workflows/production-smoke.yml`: `11:30` hằng ngày, kiểm tra source ↔ SharePoint và one-shot bounded recovery.
- `.github/workflows/operations-health.yml`: `08:20` hằng ngày, watchdog production (report sync, full-month rebuild, production smoke).
- Chỉ chạy thủ công:
  - `.github/workflows/historical-reconcile.yml`: full rebuild tuần tự toàn bộ các tháng đã hoàn tất từ `2026-06`, để bắt thay đổi lịch sử nằm ngoài mọi lookback ngắn hạn.
  - `.github/workflows/mobiwork-rebuild-month.yml`: full-month rebuild (cũng được recovery dispatcher gọi); chạy được cả khi bootstrap state chưa complete.
  - `.github/workflows/mobiwork-bootstrap-history.yml`: bootstrap lịch sử.
- `.github/workflows/ci.yml`: compile, Ruff, unit tests và coverage.

Các writer production dùng chung concurrency lock và `cancel-in-progress: false`, vì vậy một job repair/rebuild sẽ **chờ** writer hiện tại hoàn tất thay vì cắt ngang một lần ghi SharePoint đang chạy.

> GitHub chỉ giữ **một** run đang chờ trong mỗi concurrency group: khi có run mới xếp hàng, run *đang chờ* cũ hơn bị hủy (`cancelled`). Vì vậy `operations-health.yml` cảnh báo khi lần full-month rebuild gần nhất không `success` hoặc không có rebuild thành công trong 8 ngày. Khi nhận cảnh báo này, chạy lại `MobiWork Full Month Rebuild` cho tháng bị ảnh hưởng.

### Cấu hình SharePoint và xác thực

- Đích SharePoint khai báo một lần ở `env:` đầu mỗi workflow, đọc từ repository variables `SHAREPOINT_HOST`, `SHAREPOINT_SITE_PATH`, `SHAREPOINT_LIBRARY` (mặc định là production hiện tại). Đổi site/thư viện chỉ cần sửa variables, không sửa workflow.
- Drive id được resolve bởi composite action `.github/actions/resolve-sharepoint-drive`.
- Graph token trong Python được lấy bằng **GitHub OIDC client assertion mới cho mỗi lần xin token** (`ClientAssertionCredential`). Token OIDC của `azure/login` chỉ sống khoảng 5 phút, nên trước đây các job dài (rebuild cả tháng mất ~40 phút lấy dữ liệu) fail với `AADSTS700024` ở lần ghi SharePoint đầu tiên. Chạy cục bộ vẫn dùng `az login` (Azure CLI).
- Action bên thứ ba được pin theo commit SHA; Dependabot cập nhật SHA kèm comment phiên bản.

## Chạy cục bộ

```powershell
python -m pip install -r requirements.txt
python src\run_all_reports.py
python src\run_data_cham_anh.py
```

Sao chép `.env.example` thành `.env` và điền thông tin MobiWork/SharePoint trước khi chạy. Không commit `.env`, token, dữ liệu khách hàng, ảnh hoặc file export.

## Kiểm tra trước khi merge

```powershell
python -m pip install -r requirements-dev.txt
python -m compileall -q src tests
ruff check .
coverage run -m unittest discover -s tests -v
coverage report
```

## Vận hành

- Runbook: [`docs/OPERATIONS.md`](docs/OPERATIONS.md)
- Data contract: [`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md)
- Security: [`SECURITY.md`](SECURITY.md)

Báo cáo chi tiết CTKM theo KH được build riêng bởi `src/promotion_detail.py` từ
chi tiết Bill, theo 26 cột mẫu. W là tiền trước VAT; Z là hàng tặng thực tế. Một
đơn có nhiều CTKM được ghi chung một ô ở dòng bán, giữ mỗi dòng bán đúng một lần;
hàng tặng là dòng riêng để không nhân đôi số liệu. Xem quy tắc mapping, ĐVT và gate
publish tại [Operations](docs/OPERATIONS.md#báo-cáo-chi-tiết-ctkm-theo-khách-hàng).
