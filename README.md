# MobiWork DMS → SharePoint

Production data pipeline bằng Python để đồng bộ báo cáo và ảnh gốc từ MobiWork DMS sang thư viện SharePoint `MobiWorkDMS`.

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
        └─ Image metadata từ Visit master
                  │
                  ▼
          Data anh/YYYY-MM/...
```

Dự án chỉ tạo **nguồn dữ liệu chuẩn**. Nó không chấm điểm ảnh và không tạo KPI nghiệp vụ.

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
- Staged SharePoint upload → semantic verification → promote → rollback/backup khi cần.
- Không ghi lại workbook nếu nội dung nghiệp vụ không đổi.
- Full-month rebuild không đọc master cũ; fetch lại toàn bộ ngày từ MobiWork.
- Full-month rebuild có **global source gate**: tất cả report/tất cả ngày phải build thành công trước lần ghi SharePoint đầu tiên.
- Nếu publish SharePoint lỗi, các report phía sau bị chặn để giảm trạng thái nửa cũ/nửa mới.
- Production smoke fetch lại MobiWork và so trực tiếp partition SharePoint với source mới.
- Operations health mở GitHub issue khi automation production mất freshness/consistency.

## Tự động hóa

- `.github/workflows/mobiwork-bootstrap-history.yml`
  - one-time historical bootstrap trước khi production schedule tiếp tục.
- `.github/workflows/mobiwork-sync.yml`
  - `HH:05`: refresh `today`.
  - `09:00`: refresh `yesterday` và queue image sync.
- `.github/workflows/nightly-reconcile.yml`
  - `23:30`: reconcile lại **14 ngày đã hoàn tất**.
- `.github/workflows/recovery-rebuild.yml`
  - Chủ nhật `02:00`: full rebuild tháng hiện tại.
  - Chủ nhật `05:00`: full rebuild tháng trước để bắt late/back-dated edits.
  - Ngày 2 mỗi tháng `03:30`: full rebuild tháng trước để khóa sổ.
- `.github/workflows/historical-reconcile.yml`
  - Ngày 3 mỗi tháng `04:30`: full rebuild tuần tự **toàn bộ các tháng đã hoàn tất từ 2026-06 đến tháng trước**, nhằm bắt các thay đổi lịch sử nằm ngoài mọi lookback ngắn hạn.
- `.github/workflows/mobiwork-rebuild-month.yml`: full-month rebuild thủ công/được recovery dispatcher gọi; có thể chạy ngay cả khi bootstrap state chưa complete.
- `.github/workflows/mobiwork-images.yml`: đồng bộ ảnh theo batch + checkpoint.
- `.github/workflows/production-smoke.yml`: kiểm tra source ↔ SharePoint và one-shot bounded recovery.
- `.github/workflows/operations-health.yml`: watchdog production.
- `.github/workflows/ci.yml`: compile, Ruff, unit tests và coverage.

Các writer production dùng chung concurrency lock và `cancel-in-progress: false`, vì vậy một job repair/rebuild sẽ **chờ** writer hiện tại hoàn tất thay vì cắt ngang một lần ghi SharePoint đang chạy.

## Chạy cục bộ

```powershell
python -m pip install -r requirements.txt
python src\run_all_reports.py
python src\run_images.py
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
- Image sync: [`docs/image-sync.md`](docs/image-sync.md)
- Security: [`SECURITY.md`](SECURITY.md)
