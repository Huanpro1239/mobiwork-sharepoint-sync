# Kiến trúc và cách chạy chung

## Điểm vào

`python src/pipeline.py --scope all_reports` là lệnh dùng trong workflow hằng ngày.
Pipeline gọi các bộ xuất hiện có theo thứ tự:

1. `run_all_reports`: viếng thăm, khách hàng mở mới, đơn đặt hàng, đơn bán hàng.
2. `run_data_cham_anh`: ghép link ảnh viếng thăm và dữ liệu đơn bán từ monthly master.
3. `promotion_bonus`: tính và xuất trả thưởng theo khách/chương trình.
4. `promotion_detail`: chi tiết CTKM theo 26 cột của mẫu.

`promotion_history` chỉ chạy hai bước trả thưởng/CTKM, cùng danh sách tháng
`PROMOTION_BONUS_MONTHS` (`YYYY-MM,YYYY-MM` hoặc `all_existing`). Nếu hai biến
chọn tháng của hai báo cáo khác nhau, pipeline dừng trước khi tải hoặc ghi dữ liệu.
`promotion_reports` tính lại trả thưởng hiện tại và CTKM từ master Bill đã cập nhật,
dùng cùng bộ danh mục và kiểm tra xuất bản của pipeline; phù hợp sau khi bổ sung mapping.
`promotion_bonus_only` chỉ chạy trả thưởng. Audit API là công cụ chẩn đoán riêng.

Các lệnh cũ vẫn gọi được độc lập. Các công cụ bootstrap, rebuild, backfill,
reconcile và smoke tiếp tục dùng những bộ xử lý cũ để phục hồi lịch sử và kiểm tra nguồn.
Chúng dùng chung lock ghi SharePoint với workflow thường ngày.

## Phân chia trách nhiệm

| Thành phần | Trách nhiệm |
|---|---|
| `mobiwork`, `sharepoint`, `sharepoint_semantic` | Xác thực, request, retry, kiểm tra/publish dữ liệu |
| `main`, `monthly_master` | Cấu hình báo cáo nguồn, partition theo ngày, ghép/rebuild monthly master |
| `api_contract` | Kiểm tra cấu trúc danh sách và tổng bản ghi API |
| `promotion_catalogue`, `customer_catalogue`, `sales_structure` | Đọc danh mục, cấu hình và quan hệ nguồn |
| `promotion_models` | Kiểu dữ liệu trả thưởng và chuyển giá trị nguồn |
| `promotion_reward_report` | Sổ tiền/quà theo khách, phạm vi chương trình và CTKM thực tế trên đơn |
| `promotion_bonus_calc` | Tính điều kiện từ đơn bán, chỉ tiêu, trưng bày |
| `promotion_detail` | Quy tắc dòng bán/quà, trường báo cáo, đơn vị và luồng xuất CTKM |
| `excel_export`, `promotion_workbook` | Ghi Excel nguyên tử, định dạng và cơ cấu mẫu |
| `report_context` | Chia sẻ bản danh mục thành công trong một lượt pipeline |
| `report_runtime` | Đọc cờ môi trường, ghi manifest nguyên tử |
| `pipeline` | Chọn phạm vi, điều phối, kiểm tra kết quả và ghi trạng thái từng bước |

Danh mục và writer không nhập runner trả thưởng. Bộ tính trả thưởng dùng mô hình
độc lập, không nhập adapter web. Các import tương thích ở lệnh cũ giữ giao diện
đang dùng; triển khai dùng chung chỉ có một bản.

## Bản danh mục trong một lượt chạy

`CatalogueCache` được tạo mới cho mỗi lần gọi pipeline. Trả thưởng và CTKM dùng
cùng bản đọc thành công cho khách hàng, sản phẩm, nhân viên và mapping bổ sung.
Khóa danh mục có cấu hình ảnh hưởng đến phép ánh xạ; thay đổi cấu hình tạo lượt đọc mới.
Mỗi consumer nhận bản sao riêng để override ở một báo cáo không sửa báo cáo khác.
Request lỗi không được lưu cache. Không ghi danh mục khách vào file cache hoặc artifact.
`catalogue_loads` trong manifest cho biết số lần đọc thành công.

## Lỗi và chất lượng nghiệp vụ

Pipeline dừng khi bước nguồn lỗi, trả kết quả thất bại hoặc báo xuất thiếu file.
Các bước phụ thuộc được ghi `skipped`; các bước đã xuất thành công vẫn giữ trạng thái
để vận hành biết cần chạy lại phần nào. Việc ghi nhiều workbook không phải một
transaction toàn hệ thống; cơ chế staged publish và kiểm tra nội dung của từng bộ
xuất vẫn giữ nguyên.

Bốn báo cáo nguồn vẫn được thử độc lập trong `run_all_reports`: lỗi một báo cáo
không bỏ qua việc lấy các báo cáo nguồn còn lại, nhưng kết quả tổng thất bại sẽ
chặn các bước ảnh/trả thưởng/CTKM phía sau. Retry mạng có giới hạn; lỗi cấu trúc,
tổng bản ghi, khóa dữ liệu, giới hạn Excel hoặc nội dung publish không khớp đều
giữ nguyên cơ chế từ chối dữ liệu của bộ xuất nguồn.

Microsoft Graph `423 Locked` được retry có giới hạn, theo `Retry-After` nếu có hoặc
backoff hiện tại. Việc thay file vẫn giữ `If-Match` và rollback; không ép bỏ khóa.
Nếu file bị sửa trong lúc chờ thì `412` dừng cập nhật. Khóa kéo dài vẫn báo lỗi xuất
bản để người dùng đóng file rồi chạy lại, không đổi tên thành một báo cáo trùng.

`status=success` xác nhận lệnh hoàn tất. `needs_review=true` ghi rõ các mục nghiệp vụ
chưa đủ dữ liệu; không tự xác nhận trả thưởng hoặc xóa `CanBoSung` để làm báo cáo sạch.
Thiếu SS được để trống theo cấu hình; thiếu quy cách, kỳ tích lũy, phương pháp tính,
kết quả trưng bày hoặc vượt số suất được giữ theo các gate hiện có.

`output/pipeline_manifest.json` ghi phạm vi, trạng thái/thời gian từng bước và số lần
đọc danh mục. Manifest của từng bộ tiếp tục ghi nguồn, tháng, số dòng và kiểm chứng
publish. GitHub chỉ lưu manifest kiểm tra; workbook/danh mục có dữ liệu khách không
được upload dưới dạng Actions artifact.

## Build và kiểm tra

```powershell
python -m pip install -r requirements-dev.txt
python -m compileall -q src tests
python src/pipeline.py --check
ruff check .
coverage run -m unittest discover -s tests -v
coverage report
```

`--check` kiểm tra import, cấu hình bốn báo cáo và cơ cấu 26 cột, không cần token,
không gọi API và không ghi file. CI chạy các kiểm tra này trước khi merge.
Production chạy compile và preflight trước xác thực/tải dữ liệu; unit tests nằm trong CI.

## Lưu trữ và xác thực

Bốn báo cáo nguồn có một workbook chuẩn cho mỗi báo cáo/tháng. Cột ẩn `_sync_date`
giữ partition để thay dữ liệu của đúng ngày; không tạo lịch sử file ngày riêng.
Report phẳng dùng sheet `Data`; report đơn dùng `DonHang` (khóa `ma_phieu`) và
`ChiTietSP` (khóa `ma_phieu + stt`). Khi thiếu master của tháng, luồng nguồn rebuild
tháng theo các gate hiện có. Monthly master và JSON `_sync_runs` là dữ liệu nguồn
để các báo cáo tổng hợp kiểm tra và chạy lại.

Token MobiWork và thông tin xác thực Microsoft chỉ được cấp lúc chạy. Repo không
chứa token, dữ liệu khách hoặc workbook xuất. Khi triển khai sang site khác,
cần cấu hình lại thư viện SharePoint và danh mục báo cáo đích.
