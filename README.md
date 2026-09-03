# MobiWork DMS → SharePoint

Production data pipeline bằng Python để đồng bộ báo cáo và ảnh gốc từ MobiWork DMS sang thư viện SharePoint `MobiWorkDMS`.

```text
MobiWork Open API
        │
        ├─ Report fetch + validation
        │        │
        │        ├─ business-key validation
        │        ├─ employee-region enrichment cho Visit
        │        └─ monthly merge / full-month rebuild
        │                 │
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
resume hourly/nightly/weekly automation
```

Một bootstrap production (`dry_run=false`) sẽ:

- giữ production writer lock trong toàn bộ lần chạy;
- tạm **disable** các workflow routine trước khi rebuild;
- rebuild từng tháng theo thứ tự cũ → mới;
- mỗi tháng phải pass source completeness gate cho toàn bộ report;
- dừng ngay trước các tháng sau nếu có một tháng lỗi;
- ghi trạng thái vào `_sync_state/bootstrap.json`;
- chỉ **enable lại** routine workflows sau khi tất cả tháng hoàn tất thành công.

Nếu bootstrap fail hoặc bị cancel, routine automation vẫn bị pause để không cập nhật tiếp trên một historical baseline chưa đầy đủ. Sau khi sửa nguyên nhân, chạy lại bootstrap từ tháng chưa hoàn tất hoặc từ đầu range cần xác nhận.

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

Vùng được xác định từ `ma_nv` theo `config/employee_regions.json`. Nếu xuất hiện mã nhân viên chưa được mapping, Visit fail ở strict mode thay vì âm thầm phân sai vùng.

> Dashboard/Power BI phải dùng cột `vung` để lọc Vùng. Không dùng `loai_kh` thay cho Vùng.

Chi tiết xem [`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md).

## Cơ chế bảo vệ dữ liệu

- Một workbook canonical cho mỗi report/tháng; `_sync_date` lưu partition ngày và được ẩn trong Excel.
- Upsert cross-day dùng `upsert_keys` khai báo rõ trong `reports.json`, không suy luận từ tên cột.
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
  - `23:30`: reconcile lại **7 ngày đã hoàn tất**.
- `.github/workflows/recovery-rebuild.yml`
  - Chủ nhật `02:00`: queue full rebuild tháng hiện tại.
  - Ngày 1 mỗi tháng `02:30`: queue full rebuild tháng trước để khóa sổ.
- `.github/workflows/mobiwork-rebuild-month.yml`: full-month rebuild thủ công/được recovery dispatcher gọi.
- `.github/workflows/mobiwork-images.yml`: đồng bộ ảnh theo batch + checkpoint.
- `.github/workflows/production-smoke.yml`: kiểm tra source ↔ SharePoint và one-shot bounded recovery.
- `.github/workflows/operations-health.yml`: watchdog production.
- `.github/workflows/ci.yml`: compile, Ruff, unit tests và coverage.

Các writer production dùng concurrency lock để không đồng thời sửa cùng vùng SharePoint. Full-month recovery và bootstrap được ưu tiên khi cần khôi phục dữ liệu.

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
