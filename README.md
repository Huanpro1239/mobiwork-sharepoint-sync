# MobiWork → SharePoint → Image AI → Sales KPI

Pipeline Python phục vụ vận hành thực tế cho luồng **MobiWork DMS → Microsoft SharePoint → Chấm ảnh AI V2.3 → KPI bán hàng V2.4**.

Phần đồng bộ báo cáo và hình ảnh được thiết kế nhẹ để có thể chạy trên GitHub-hosted runner. Phần suy luận AI nặng chạy trên Windows self-hosted runner cố định, gắn nhãn `dms-ai`.

## 1. Kiến trúc hệ thống

```text
MobiWork Open API
   ├─ đồng bộ báo cáo ─────► File master theo tháng trên SharePoint
   └─ đồng bộ hình ảnh ─────► SharePoint Data anh/YYYY-MM/...
                                   │
                                   ▼
                          Windows self-hosted runner
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
             Image Scoring V2.3              Sales KPI V2.4
             CLIP + YOLO + OCR              Visit/Order M-1 + M
             evidence + quality gate        tồn kho + bằng chứng ảnh
                    │                             │
                    │                  Customer History dạng gọn
                    │                  (1 dòng / Mã KH)
                    └──────────────┬──────────────┘
                                   ▼
                         File Excel KPI dùng công thức sống
                                   ▼
                          SharePoint KPI/YYYY-MM/
```

## 2. Các cơ chế kiểm soát chính

- Khi thay file trên SharePoint, hệ thống dùng cơ chế staged replacement và kiểm tra cấu trúc workbook trước khi ghi đè.
- Phân loại khách hàng Mới/Cũ sử dụng file lịch sử gọn `KPI/History/customer_history.csv`, mỗi khách hàng chỉ có một dòng.
- Lần chạy đầu tiên có thể bootstrap lịch sử từ các file master theo tháng, xử lý từng workbook một để tránh tốn RAM.
- Các lần chạy sau chỉ cần lấy dữ liệu tháng M-1 và M để tính KPI và cập nhật lịch sử tăng dần.
- Dữ liệu M-1 và M được nối theo `ma_kh`.
- Nhân viên sở hữu KPI là nhân viên đi viếng thăm khách hàng trong tháng M.
- `ma_phieu` là khóa chính để nhận diện đơn hàng; thông tin cũ trong `dien_giai [...]` chỉ được dùng làm phương án dự phòng.
- Các dòng sản phẩm khuyến mãi có `is_km=True` không được cộng vào sản lượng KPI hoặc lịch sử mua hàng.
- Điểm chấm ảnh được cache theo **chữ ký model + SHA256 của nội dung ảnh**.
- Có thể dùng các file điểm tháng M-1/M trước đó để khởi tạo cache cho runner mới.
- Lỗi kỹ thuật khi chấm ảnh được ghi là `Khong_the_cham`, không bị quy thành kết quả kinh doanh `Khong_dat`.
- Nhãn sửa tay trong sheet `Chi_tiet_Anh_Checkin` được giữ lại khi xuất file KPI mới và công thức Excel sẽ tính lại trực tiếp.
- Model weights, ảnh tham chiếu, ảnh DMS, secrets và file KPI đầu ra không được đưa vào Git history.

## 3. Cấu trúc repository

```text
src/
├─ mobiwork.py / sharepoint.py / image_sync.py   đồng bộ MobiWork và SharePoint
├─ scoring/                                      Chấm ảnh AI V2.3
│  ├─ classifier.py / modeling.py
│  ├─ decision_policy.py / image_scoring.py
│  ├─ yolo_verifier.py / ocr_engine.py / face_detector.py
│  ├─ assets.py / score_cache.py / records.py
│  └─ ...
├─ kpi/                                          KPI V2.4
│  ├─ customer_aggregator.py
│  ├─ customer_history.py                        lịch sử khách hàng dạng gọn
│  ├─ kpi_rules.py
│  ├─ manual_labels.py / workbook_formulas.py
│  ├─ output_contract.py                         kiểm tra cấu trúc file KPI đầu ra
│  └─ kpi_exporter.py
├─ sharepoint_kpi_source.py
├─ score_kpi_pipeline.py
├─ run_score_kpi.py
└─ bootstrap_model_assets.py
```

## 4. Chấm ảnh AI V2.3

V2.3 sử dụng bốn nhóm điểm chính:

1. Scene: nhận diện loại bối cảnh.
2. Sign validity: kiểm tra biển hiệu.
3. Display validity: kiểm tra trưng bày.
4. Fraud: phát hiện ảnh có dấu hiệu bất thường/gian lận.

Ngưỡng mặc định theo hướng thận trọng:

| Điều kiện | Giá trị mặc định |
|---|---:|
| Tự động đạt | `>= 0.88` |
| Tự động loại do validity thấp | `<= 0.05` |
| Đưa vào kiểm tra fraud | `>= 0.60` |
| Ứng viên auto-fail fraud | `>= 0.975` |
| Độ tương đồng ảnh tham chiếu | `>= 0.70` |
| Biên mơ hồ scene | `0.08` |
| Precision tối thiểu cho auto-decision OOF | `>= 99%` |

YOLO/OCR có nhiệm vụ bổ sung bằng chứng để xác nhận ứng viên hoặc xử lý scene mơ hồ. YOLO/OCR không được phép bỏ qua các quality gate, validity gate, novelty gate hoặc fraud gate.

## 5. KPI bán hàng V2.4

Khách hàng chỉ được đưa vào KPI tháng M khi có ít nhất một lượt viếng thăm trong tháng M. Bằng chứng có thể được cộng từ tháng M-1 và M.

Quy tắc chính:

- **KHTC:** có ít nhất một đơn hàng thật đạt ngưỡng đơn lớn nhất cấu hình, mặc định `3.0 KTB`.
- **KHĐĐK:** nếu không đạt KHTC thì tổng sản lượng M-1/M phải đạt ngưỡng cấu hình, mặc định `5.0 KTB`.
- Có ít nhất một lần `ghi_ton` trong M-1/M.
- Có ít nhất một bằng chứng `Bien_hieu` hợp lệ và một bằng chứng `Trung_bay` hợp lệ trong M-1/M.
- Ghi chú hợp lệ có thể thay thế bằng chứng biển hiệu trong một số trường hợp nhưng không được thay thế bằng chứng trưng bày.
- Mới/Cũ được xác định từ `first_activity_date` trong file lịch sử khách hàng.

Quy tắc phân loại khách hàng cho tháng M:

```text
first_activity_date < ngày đầu tháng M  → Cũ
first_activity_date >= ngày đầu tháng M → Mới
không có first_activity_date            → Không rõ
```

File lịch sử khách hàng được lưu tại:

```text
KPI/History/customer_history.csv
```

Lần chạy thành công đầu tiên, hệ thống có thể đọc tuần tự các master Visit/Order lịch sử và tạo file history chỉ một dòng cho mỗi `ma_kh`. Các lần chạy sau chỉ tải workbook M-1/M và cập nhật lịch sử tăng dần, đồng thời không bao giờ đẩy `first_activity_date` sang ngày muộn hơn.

Chi tiết xem `docs/CUSTOMER_HISTORY.md` và `docs/KPI_RULES_V2_4.md`.

Ngày công và tiền thưởng được tính bằng công thức Excel sống, có xử lý ngày Chủ nhật và giới hạn mức thưởng.

## 6. Tài nguyên AI riêng tư

Các tài nguyên AI riêng tư được lưu trên SharePoint, không lưu trong Git:

```text
Model Assets/
├─ reference/...
├─ reference_overrides.csv
├─ weights/yolov8s-world.pt
└─ template/KPI_template.xlsx
```

Khởi tạo một lần từ dự án local cũ:

```powershell
python src\bootstrap_model_assets.py --source "D:\DMS cham anh" --dry-run
python src\bootstrap_model_assets.py --source "D:\DMS cham anh"
```

Nên chạy `--dry-run` trước để kiểm tra danh sách file trước khi upload thật.

## 7. Cách chạy

### Đồng bộ báo cáo MobiWork

```powershell
python src\run_all_reports.py
```

### Đồng bộ hình ảnh rolling

```powershell
python src\run_images.py
```

### Chấm ảnh AI và tính KPI

```powershell
pip install -r requirements-ai.txt
python src\run_score_kpi.py
```

### Kiểm tra một tháng lịch sử nhưng không upload

Ví dụ kiểm tra tháng 08/2026:

```powershell
python src\run_score_kpi.py --period 2026-08 --dry-run
```

Ở chế độ dry-run, hệ thống có thể tạo file local `runtime/output/customer_history.csv` để kiểm tra nhưng không publish history master hoặc KPI output lên SharePoint.

## 8. Đầu ra trên SharePoint

```text
KPI/
├─ History/
│  └─ customer_history.csv
└─ YYYY-MM/
   ├─ Ket_qua_cham_cong_va_thuong_KPI.xlsx
   ├─ Ket_qua_Chi_tiet_Anh.csv
   └─ run_manifest.json
```

Trước khi xuất KPI mới, hệ thống tải workbook tháng hiện có nếu có để giữ lại các giá trị `Nhãn Sửa Tay`.

File history chỉ được upload ở giai đoạn publish khi không chạy `--dry-run`.

## 9. Kiểm tra an toàn file KPI

File KPI đầu ra có hợp đồng cấu trúc cố định gồm 5 sheet:

```text
Tong_hop_KPI_Nhan_vien
Chi_tiet_Khach_hang
Chi_tiet_Anh_Checkin
Canh_bao
Tham_so
```

Trước khi thay file sản xuất, hệ thống kiểm tra cấu trúc workbook trong bộ nhớ và mở lại file XLSX vừa serialize để phát hiện lỗi quan hệ, công thức hoặc sheet trước khi ghi đè SharePoint.

Nếu kiểm tra thất bại, hệ thống dừng theo hướng fail-closed thay vì publish một workbook hỏng.

## 10. Giới hạn thời gian chấm AI và cơ chế resume

Luồng production được chia thành batch giới hạn thay vì cố chấm toàn bộ backlog trong một lần.

Mục tiêu:

- tránh GitHub Actions hoặc self-hosted runner bị hard timeout;
- lưu checkpoint sau từng batch;
- có thể chạy tiếp phần backlog còn lại;
- không phải chấm lại các ảnh đã có cache hợp lệ.

Workflow production hiện hỗ trợ các biến giới hạn số ảnh, thời gian runtime, chunk size và số worker tải ảnh.

## 11. GitHub Actions

Các workflow chính:

- `.github/workflows/mobiwork-sync.yml`: đồng bộ các file master báo cáo.
- `.github/workflows/mobiwork-images.yml`: đồng bộ hình ảnh rolling.
- `.github/workflows/image-scoring-kpi.yml`: chấm ảnh AI + KPI trên Windows runner `dms-ai` và có thể chạy sau khi image sync thành công.
- `.github/workflows/ci.yml`: compile, Ruff, coverage và unit test nhẹ, không cài toàn bộ AI stack nặng.

Một số workflow probe/migration trong `.github/workflows/` phục vụ kiểm thử hoặc chuyển đổi dữ liệu và có thể được cấu hình chỉ chạy trong điều kiện cụ thể.

## 12. Kiểm tra code local

Cài thư viện dev:

```bash
pip install -r requirements-dev.txt
```

Chạy kiểm tra nhẹ:

```bash
python -m compileall -q src tests
ruff check .
coverage run -m unittest discover -s tests -v
coverage report
```

Kiểm tra riêng policy/model AI:

```bash
pip install -r requirements-ai.txt
python -m unittest discover -s tests_ai -v
```

## 13. Bảo mật

Không commit các dữ liệu sau vào repository:

- file `.env`;
- Microsoft access/refresh token;
- tài khoản hoặc mật khẩu MobiWork;
- dữ liệu khách hàng export;
- ảnh DMS;
- ảnh tham chiếu;
- model weights;
- database/cache runtime;
- file KPI được sinh ra;
- secret dùng cho GitHub Actions hoặc SharePoint.

Các tài nguyên runtime/private phải được loại khỏi Git bằng `.gitignore`.

Trước khi bật production, nên đọc thêm:

- `SECURITY.md`
- `docs/SELF_HOSTED_RUNNER.md`
- `docs/CUSTOMER_HISTORY.md`
- `docs/KPI_RULES_V2_4.md`

## 14. Đánh giá trạng thái hiện tại

Repository hiện đã có các thành phần chính cần thiết cho một pipeline vận hành thực tế: đồng bộ MobiWork, lưu SharePoint, đồng bộ ảnh, cache, chấm ảnh AI, KPI, lịch sử khách hàng, kiểm tra file đầu ra, workflow CI và cơ chế batch/resume.

Tuy nhiên, trước khi coi hệ thống là hoàn toàn ổn định cho production dài hạn, vẫn cần theo dõi định kỳ:

- tỷ lệ thành công của `mobiwork-sync.yml` và `mobiwork-images.yml`;
- backlog ảnh còn lại sau mỗi batch AI;
- dung lượng SharePoint và thời gian tải file;
- tình trạng online của self-hosted runner `dms-ai`;
- độ chính xác thực tế của nhãn AI so với nhãn sửa tay;
- tính đúng của KPI sau mỗi thay đổi quy tắc kinh doanh;
- cảnh báo workbook fail-closed và các lỗi dữ liệu nguồn.

Không nên đánh giá production chỉ dựa vào việc workflow có màu xanh. Cần kiểm tra đồng thời tính đầy đủ của dữ liệu SharePoint, số lượng bản ghi, số ảnh, file KPI cuối cùng và `run_manifest.json`.
