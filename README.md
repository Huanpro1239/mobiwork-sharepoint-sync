# MobiWork DMS → SharePoint

Production data pipeline bằng Python để nạp 4 báo cáo lịch sử từ MobiWork DMS Open API (viếng thăm, mở mới khách hàng, đơn đặt hàng, đơn bán hàng) vào thư viện SharePoint `MobiWorkDMS`, kèm **Data chấm ảnh**, **Báo cáo trả thưởng** và **Chi tiết CTKM theo khách hàng**. Lệnh `src/pipeline.py` điều phối toàn bộ các bộ xuất hiện có theo thứ tự và dùng chung danh mục trong mỗi lượt chạy.

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

Data chấm ảnh dùng link ảnh gốc của MobiWork. Phần trả thưởng tính điều kiện doanh số từ đơn bán và đọc kết quả trưng bày từ DMS; các điều kiện chưa đủ chứng cứ được giữ ở trạng thái cần kiểm tra.

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

Báo cáo trả thưởng được tính từ danh mục chương trình, đơn bán hàng theo ngày giao,
danh mục khách và kết quả trưng bày (`PROMOTION_BONUS_SOURCE=auto` → `calc`).
Cấu hình đích lưu tại `config/promotion_bonus.json`:

```text
06_BaoCaoTraThuong/BaoCaoTraThuong_Current.xlsx
├─ TheoDoiTichLuy: mẫu theo dõi KH tham gia, mục tiêu, kết quả và suất đạt từng tháng
├─ KhuyenMaiDonHang: mẫu chi tiết CTKM với đơn/quà thực tế DMS, giữ 26 cột A–Z
├─ TraThuong: từng khách/CT/khoản tiền hoặc quà, gồm dự kiến và đủ điều kiện
├─ BaoCao: dòng bán tính vào CT và dòng TRẢ THƯỞNG của khách đủ điều kiện
├─ Thuong_theo_don: thưởng phân bổ theo đơn/chương trình của khách đủ điều kiện
├─ ChuongTrinh: toàn bộ mức trả thưởng trong kỳ và CTKM có trên đơn, kể cả CT chưa tính được
├─ Tong_hop: chỉ tiêu và thưởng dự kiến theo khách/chương trình
├─ Ket_qua: sản phẩm thưởng của khách đủ điều kiện
├─ Kiem_tra: nguồn, kỳ và trạng thái chương trình
├─ CanBoSung: chỉ tạo khi thiếu mapping hoặc lỗi nguồn/quy đổi
└─ Can_xem: quy tắc, kỳ tích lũy, trưng bày hoặc số suất cần kiểm tra
```

Hai sheet đầu là hai góc nhìn theo các mẫu người dùng cung cấp, trong cùng file Current
và bản lưu từng tháng, được cập nhật bởi pipeline hằng ngày. `TheoDoiTichLuy` lấy toàn bộ
khách đăng ký ở các mức CT trong kỳ, kể cả chưa mua hàng. Mỗi khách/mức là 1 suất theo
xác nhận người dùng; ngày đăng ký và người liên hệ thiếu nguồn để trống. Các cột tháng
theo thời gian áp dụng CT, có năm để không lẫn hai năm. Mục tiêu và đơn vị lấy từ quy tắc
CT; số suất đạt tính riêng từng tháng hoặc phần tăng/giảm của lũy kế tùy cách tính đã khai.
Tổng tích lũy thể hiện doanh số/sản lượng từ đầu CT, còn lại là số âm chưa đạt mục tiêu
tối thiểu của kỳ xét thưởng. Thiếu tháng nguồn/ĐVT thì để trống kỳ bị ảnh hưởng và tổng,
giữ nguyên kết quả tháng đủ nguồn, ghi lý do ở `Thông tin nguồn`. Tháng tương lai/ngoài CT
để trống. Số suất đạt chưa phải xác nhận chi thưởng; trạng thái đủ điều kiện lấy từ bộ
tính hiện có. Danh sách tham gia hiện tại không xác nhận thời điểm tham gia trong quá khứ.

`BaoCao` trả thưởng giữ 26 cột A–Z của mẫu và thêm cột phân bổ thưởng theo đơn,
tiền mặt phân bổ, giá trị quà ước tính và tiền mặt trả thưởng. Cột Z chỉ chứa số lượng
quà. Cột `Tiền thưởng phân bổ (đ)` giữ tổng tiền mặt + giá trị quà ước tính để tương thích;
thiếu giá quà thì tổng này trống, nhưng tiền mặt riêng vẫn hiển thị.
`TraThuong` dùng ĐVT gốc của quà; không cộng số lượng khác ĐVT. Đủ điều kiện tính toán
chưa xác nhận đã chi tiền hoặc giao quà. Voucher giữ là quà theo mã nguồn.
`ChuongTrinh` liệt kê toàn bộ mức trong danh mục trả thưởng được API chọn cho kỳ;
phần CTKM chỉ liệt kê chương trình thực sự có trên đơn, không suy danh mục chưa phát sinh.
`BaoCao` CTKM giữ đúng 26 cột. `KhuyenMaiDonHang` trong file trả thưởng dùng cùng
quy tắc ngày đơn, mã CT và quy đổi, thêm dòng/ĐVT/số lượng/chiết khấu nguồn để đối soát.
Phần phân bổ theo đơn dùng chức năng đã tích hợp trên
main; giá trị quà hiện vật không có giá bán nguồn được để trống.

Tài liệu API chính thức: [Danh sách chương trình trả thưởng](https://dms.mobiwork.vn/openapi/#/PromotionBonus/findPromotionBonus) và [Báo cáo trả thưởng](https://dms.mobiwork.vn/openapi/#/PromotionBonusReport/findPromotionBonusReport). Chi tiết tham số và mapping nằm trong [data contract](docs/DATA_CONTRACT.md#promotion-bonus-snapshot-contract).

Workflow `MobiWork DMS Sync` cập nhật file Current và bản tháng
`06_BaoCaoTraThuong/YYYY/MM/BaoCaoTraThuong_YYYY-MM.xlsx`. `promotion_history`
tính các tháng đã có Bill monthly master, không ghi đè Current.
Danh sách đăng ký và cây phòng ban là dữ liệu hiện tại, nên chưa cam kết tái tạo
đầy đủ lịch sử hoặc khớp mọi dòng DMS. `CanBoSung` phân biệt thiếu thông tin mô tả
với lỗi chặn xuất bản; lỗi nguồn, identity hoặc hệ số không hợp lệ không được đưa lên SharePoint.
Thiếu quy cách giữ số lượng quy đổi trống, ghi rõ `unit_gaps`, không tự chọn hệ số.
Nguồn `ui` và `openapi` vẫn có thể chọn khi chạy CLI để đối chiếu; không phải nguồn mặc định.

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
python src\pipeline.py --check
python src\pipeline.py --scope all_reports
```

Lệnh chạy chung gọi lần lượt: 4 báo cáo nguồn → Data chấm ảnh → Trả thưởng → Chi tiết CTKM.
Các lệnh cũ vẫn dùng được riêng để sửa lỗi/vận hành, nhưng workflow hằng ngày chỉ gọi pipeline một lần.
Danh mục khách hàng, sản phẩm, nhân viên và mapping bổ sung được dùng chung khi cấu hình đầu vào giống nhau;
mỗi lượt chạy tải mới và chỉ giữ bản đọc thành công trong bộ nhớ. Xem [kiến trúc](docs/ARCHITECTURE.md).

Xuất lại cả trả thưởng và CTKM của tháng đã có Bill master:

```powershell
$env:PROMOTION_BONUS_MONTHS = "2026-09"
python src\pipeline.py --scope promotion_history
```

`DRY_RUN=true` không ghi SharePoint. Chạy lịch sử vẫn đọc Bill master trên SharePoint;
chạy toàn bộ ở chế độ này dùng master cục bộ vừa xuất cho các bước phụ thuộc.

Dùng `.env.example` làm danh sách biến và thiết lập thông tin MobiWork/SharePoint
trong môi trường của phiên shell trước khi chạy. Các lệnh Python không tự nạp file
`.env`; GitHub Actions lấy cấu hình từ secrets/variables. Không commit `.env`, token,
dữ liệu khách hàng, ảnh hoặc file export.

## Kiểm tra trước khi merge

```powershell
python -m pip install -r requirements-dev.txt
python -m compileall -q src tests
python src\pipeline.py --check
ruff check .
coverage run -m unittest discover -s tests -v
coverage report
```

## Vận hành

- Runbook: [`docs/OPERATIONS.md`](docs/OPERATIONS.md)
- Data contract: [`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md)
- Security: [`SECURITY.md`](SECURITY.md)

Báo cáo chi tiết CTKM theo KH do `src/promotion_detail.py` xử lý trong pipeline chung, từ
chi tiết Bill, theo 26 cột mẫu. W là tiền trước VAT; Z là hàng tặng thực tế. Một
đơn có nhiều CTKM được ghi chung một ô ở dòng bán, giữ mỗi dòng bán đúng một lần;
hàng tặng là dòng riêng để không nhân đôi số liệu. Xem quy tắc mapping, ĐVT và gate
publish tại [Operations](docs/OPERATIONS.md#báo-cáo-chi-tiết-ctkm-theo-khách-hàng).
