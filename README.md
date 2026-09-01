# MobiWork → SharePoint → Image AI → Sales KPI

Pipeline Python phục vụ vận hành thực tế cho luồng **MobiWork DMS → Microsoft SharePoint → đồng bộ ảnh → Chấm ảnh AI V2.3 → KPI bán hàng V2.4**.

Hiện tại các workflow production chính chạy trên **GitHub-hosted Ubuntu runner**. Dữ liệu nghiệp vụ, ảnh DMS, model assets và file KPI được lưu ngoài Git; SharePoint là nguồn lưu trữ chính của pipeline.

## 1. Luồng production hiện tại

```text
MobiWork Open API
        │
        ▼
MobiWork DMS Sync
(mobiwork-sync.yml)
        │
        ├──► cập nhật monthly master trên SharePoint
        │
        └──► sau khi refresh "yesterday" thành công
                 │
                 ▼
        MobiWork Daily Image Sync
        (mobiwork-images.yml)
                 │
                 ├── warming_up ──► tự chạy batch tiếp theo
                 │
                 └── success + pending=0 + failed=0
                              │
                              ▼
                    Image Scoring + Sales KPI
                    (image-scoring-kpi.yml)
                              │
                              ├── CLIP + YOLO + OCR
                              ├── Customer History
                              └── Excel KPI có công thức sống
                                      │
                                      ▼
                              SharePoint KPI/YYYY-MM/
```

**Nguyên tắc quan trọng:** KPI production không được chạy chỉ vì workflow ảnh kết thúc. KPI chỉ được dispatch khi manifest của image sync xác nhận ảnh đã reconcile hoàn tất, không còn pending và không có download failure.

## 2. Nguồn dữ liệu và monthly master

Các báo cáo được cấu hình tại `config/reports.json`:

| Key | Báo cáo | Thư mục SharePoint |
|---|---|---|
| `visit` | Báo cáo viếng thăm | `01_BaoCaoViengTham` |
| `new_customer` | Mở mới khách hàng | `02_MoMoiKhachHang` |
| `order` | Đơn đặt hàng | `03_DonDatHang` |
| `bill` | Đơn bán hàng | `04_DonBanHang` |

Mỗi báo cáo được lưu thành **một file master cho mỗi tháng**:

```text
<Folder>/<YYYY>/<MM>/<ReportName>_YYYY-MM.xlsx
```

Khi đồng bộ một ngày, partition `_sync_date` của ngày đó được thay mới trong master tháng thay vì nối mù vào cuối file. Nếu master tháng bị thiếu, hệ thống có thể rebuild từ ngày đầu tháng đến ngày mục tiêu.

Nếu một report lỗi, các report còn lại vẫn được chạy để tạo audit đầy đủ, nhưng manifest cuối cùng chuyển sang `partial_failure` và workflow production fail. Vì vậy bước image sync phía sau không được dispatch từ một lần refresh report chưa hoàn chỉnh.

## 3. Đồng bộ SharePoint an toàn

Luồng ghi file sử dụng staged replacement và semantic verification để giảm nguy cơ ghi đè workbook hỏng.

Các kiểm soát chính:

- kiểm tra file target có phải folder hay không;
- upload file tạm trước khi thay file production;
- kiểm tra lại nội dung workbook sau khi upload;
- chỉ thay file chính khi verification đạt;
- giữ audit manifest của từng lần chạy;
- dọn các file report legacy sau khi canonical monthly master đã được ghi thành công.

## 4. Đồng bộ hình ảnh

Metadata ảnh không đọc trực tiếp lại từ API MobiWork để tính KPI. Image sync lấy metadata từ **monthly master viếng thăm đã được lưu trên SharePoint**, nhờ đó báo cáo và ảnh dùng chung một nguồn dữ liệu đã persist.

Ảnh được lưu theo cấu trúc:

```text
Data anh/YYYY-MM/<Nhân viên>/<Mã KH>/...
```

Identity ảnh sử dụng:

```text
ngày nghiệp vụ + SHA256(URL ảnh MobiWork)
```

Điều này có hai mục đích:

- cùng một ảnh trong cùng ngày không bị tải lặp chỉ vì `stt_hinh` hoặc đuôi file thay đổi;
- nếu cùng URL xuất hiện ở **hai ngày viếng thăm khác nhau**, hai bằng chứng vẫn được giữ riêng, không bị mất ảnh ngày sau.

Image sync có state tại SharePoint, overlap khi chạy incremental, retry cursor, batch limit và soft runtime budget. Khi backlog chưa hết, trạng thái là `warming_up`; workflow tự dispatch batch tiếp theo.

KPI chỉ được gọi khi image sync đạt đồng thời:

```text
status = success
pending_remaining = 0
failed_count = 0
dry_run = false
```

## 5. Cơ chế queue của GitHub Actions

Các workflow ghi SharePoint dùng chung concurrency group:

```text
mobiwork-sharepoint-production
```

Cấu hình production dùng `queue: max` và `cancel-in-progress: false` để các run đang chờ không bị run mới thay thế. Điều này đặc biệt quan trọng vì report chạy theo giờ, trong khi refresh ngày hôm qua và image reconciliation có thể phải xếp hàng chờ.

Luồng AI/KPI sử dụng concurrency group riêng `mobiwork-kpi-production`, cũng chạy tuần tự để tránh hai lần publish KPI cùng lúc.

## 6. Chấm ảnh AI V2.3

V2.3 sử dụng nhiều nguồn bằng chứng:

1. **Scene** – nhận diện loại bối cảnh.
2. **Sign validity** – kiểm tra biển hiệu.
3. **Display validity** – kiểm tra trưng bày.
4. **Fraud** – phát hiện dấu hiệu ảnh bất thường/gian lận.
5. **YOLO/OCR** – bổ sung bằng chứng cho quyết định.

Các ngưỡng mặc định theo hướng thận trọng:

| Điều kiện | Giá trị mặc định |
|---|---:|
| Tự động đạt | `>= 0.88` |
| Tự động loại do validity thấp | `<= 0.05` |
| Đưa vào kiểm tra fraud | `>= 0.60` |
| Ứng viên auto-fail fraud | `>= 0.975` |
| Độ tương đồng ảnh tham chiếu | `>= 0.70` |
| Biên mơ hồ scene | `0.08` |
| Precision tối thiểu cho auto-decision OOF | `>= 99%` |

YOLO/OCR không được phép bỏ qua novelty, fraud, validity hoặc quality gate.

Điểm chấm được cache theo **model/pipeline signature + SHA256 nội dung ảnh**. Lỗi kỹ thuật khi chấm ảnh được ghi là `Khong_the_cham`, không tự động biến thành kết quả kinh doanh `Khong_dat`.

## 7. KPI bán hàng V2.4

Khách hàng được xét KPI tháng M khi có viếng thăm trong tháng M. Bằng chứng có thể cộng trong M-1 và M.

Quy tắc chính:

- **KHTC:** có ít nhất một đơn thật đạt ngưỡng đơn lớn nhất, mặc định `3.0 KTB`.
- **KHĐĐK:** nếu không đạt KHTC thì tổng sản lượng M-1/M phải đạt ngưỡng cấu hình, mặc định `5.0 KTB`.
- Có ít nhất một lần `ghi_ton` trong M-1/M.
- Có bằng chứng `Bien_hieu` và `Trung_bay` hợp lệ trong M-1/M theo rule hiện hành.
- Ghi chú hợp lệ có thể thay thế bằng chứng biển hiệu trong trường hợp được rule cho phép, nhưng không thay bằng chứng trưng bày.
- Dòng sản phẩm khuyến mãi `is_km=True` không được cộng vào sản lượng KPI/history facts.
- `ma_phieu` là identity chính của đơn hàng; `dien_giai [...]` chỉ là fallback tương thích dữ liệu cũ.

Phân loại Mới/Cũ:

```text
first_activity_date < ngày đầu tháng M  → Cũ
first_activity_date >= ngày đầu tháng M → Mới
không có first_activity_date            → Không rõ
```

Customer history được lưu tại:

```text
KPI/History/customer_history.csv
```

History chỉ giữ một dòng cho mỗi `ma_kh`. Sau bootstrap ban đầu, các lần chạy sau cập nhật tăng dần và không được đẩy `first_activity_date` sang ngày muộn hơn.

## 8. File Excel KPI và fail-closed validation

Workbook KPI production có hợp đồng 5 sheet:

```text
Tong_hop_KPI_Nhan_vien
Chi_tiet_Khach_hang
Chi_tiet_Anh_Checkin
Canh_bao
Tham_so
```

Trước khi publish, hệ thống:

- kiểm tra cấu trúc workbook trong bộ nhớ;
- serialize ra XLSX tạm;
- mở lại file vừa ghi để kiểm tra lần nữa;
- chỉ thay file production nếu validation đạt.

Các giá trị `Nhãn Sửa Tay` trong `Chi_tiet_Anh_Checkin` được giữ lại khi re-export và các công thức Excel được đặt ở chế độ tính lại.

## 9. Cấu trúc repository chính

```text
src/
├─ mobiwork.py                 gọi MobiWork Open API + retry/pagination
├─ monthly_master.py           partition và monthly master
├─ sharepoint.py               Microsoft Graph / SharePoint
├─ sharepoint_semantic.py      staged replacement + semantic verification
├─ run_all_reports.py          report pipeline
├─ image_sync.py               rule và cấu hình ảnh
├─ image_sync_reliable.py      reconcile ảnh, state, retry, batch/resume
├─ sharepoint_image_source.py  đọc metadata ảnh từ monthly master
├─ scoring/                    Image Scoring V2.3
├─ kpi/                        Sales KPI V2.4
├─ sharepoint_kpi_source.py    dữ liệu Visit/Order + resolve ảnh cho KPI
├─ score_kpi_pipeline.py       orchestration AI/KPI
├─ run_score_kpi.py            entrypoint local
└─ run_score_kpi_cloud.py      entrypoint GitHub-hosted production
```

## 10. GitHub Actions

Các workflow chính:

- `.github/workflows/mobiwork-sync.yml` – cập nhật report master; chạy hourly và refresh `yesterday` hằng ngày.
- `.github/workflows/mobiwork-images.yml` – image reconciliation; production được report workflow dispatch sau refresh `yesterday` thành công, đồng thời tự chạy tiếp khi còn backlog.
- `.github/workflows/image-scoring-kpi.yml` – chấm ảnh + KPI; production chỉ được image workflow dispatch sau khi reconcile ảnh hoàn tất.
- `.github/workflows/ci.yml` – compile, Ruff, unit test, coverage và các regression test.

Một số workflow probe/migration trong `.github/workflows/` dùng cho kiểm thử hoặc chuyển đổi dữ liệu và chỉ chạy khi điều kiện tương ứng được đáp ứng.

## 11. Cách chạy local

Cài runtime nhẹ:

```powershell
pip install -r requirements.txt
```

Đồng bộ report:

```powershell
python src\run_all_reports.py
```

Đồng bộ ảnh:

```powershell
python src\run_images.py
```

Chấm ảnh + KPI:

```powershell
pip install -r requirements-ai.txt
python src\run_score_kpi.py
```

Kiểm tra một tháng nhưng không publish:

```powershell
python src\run_score_kpi.py --period 2026-08 --dry-run
```

## 12. Đầu ra KPI

```text
KPI/
├─ History/
│  └─ customer_history.csv
└─ YYYY-MM/
   ├─ Ket_qua_cham_cong_va_thuong_KPI.xlsx
   ├─ Ket_qua_Chi_tiet_Anh.csv
   └─ run_manifest.json
```

Ở `--dry-run`, output được tạo local để kiểm tra nhưng không publish KPI/history production lên SharePoint.

## 13. Kiểm tra code

Cài thư viện dev:

```bash
pip install -r requirements-dev.txt
```

Chạy kiểm tra:

```bash
python -m compileall -q src tests tests_ai
ruff check .
coverage run -m unittest discover -s tests -v
coverage report
```

Kiểm tra riêng policy/model AI:

```bash
pip install -r requirements-ai.txt
python -m unittest discover -s tests_ai -v
```

## 14. Bảo mật

Không commit vào repository:

- `.env`;
- Microsoft token hoặc credentials;
- MobiWork credentials;
- dữ liệu khách hàng export;
- ảnh DMS hoặc ảnh tham chiếu;
- model weights;
- cache/database runtime;
- file KPI production;
- GitHub Actions secrets.

Private model assets và template được lưu trên SharePoint theo cấu hình môi trường/workflow, không lưu trong Git history.

Xem thêm:

- `SECURITY.md`
- `docs/CUSTOMER_HISTORY.md`
- `docs/KPI_RULES_V2_4.md`

## 15. Trạng thái hiện tại

Pipeline hiện đã có các lớp bảo vệ quan trọng cho production:

- MobiWork API retry và kiểm tra response;
- monthly partition replacement thay vì append mù;
- SharePoint staged replacement + semantic verification;
- report failure chặn pipeline downstream;
- image reconciliation có state, batch, runtime budget và resume;
- identity ảnh thống nhất theo ngày nghiệp vụ + URL digest;
- image backlog chưa hoàn tất không được kích hoạt KPI;
- workflow production được serialize bằng concurrency queue;
- KPI workbook có fail-closed validation;
- CI có regression tests cho orchestration và image identity.

Vẫn cần theo dõi vận hành bằng `sync_manifest.json`, `image_sync_manifest.json` và `run_manifest.json`; không nên đánh giá tính đúng của production chỉ dựa trên màu xanh của GitHub Actions.
