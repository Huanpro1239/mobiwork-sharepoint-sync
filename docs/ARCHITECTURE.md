# Kiến trúc và cách chạy chung

## Điểm vào

`python src/pipeline.py --scope all_reports` là lệnh dùng trong workflow hằng ngày.
Pipeline gọi các bộ xuất theo thứ tự:

1. `run_all_reports`: viếng thăm, khách hàng mở mới, đơn đặt hàng, đơn bán hàng.
2. `run_data_cham_anh`: ghép link ảnh viếng thăm và dữ liệu đơn bán từ monthly master.

Các lệnh cũ vẫn gọi được độc lập. Các công cụ bootstrap, rebuild, backfill,
reconcile và smoke tiếp tục dùng những bộ xử lý này để phục hồi lịch sử và kiểm tra nguồn.
Chúng dùng chung lock ghi SharePoint với workflow thường ngày.

## Phân chia trách nhiệm

| Thành phần | Trách nhiệm |
|---|---|
| `mobiwork`, `sharepoint`, `sharepoint_semantic` | Xác thực, request, retry, kiểm tra/publish dữ liệu |
| `main`, `monthly_master` | Cấu hình báo cáo nguồn, partition theo ngày, ghép/rebuild monthly master |
| `api_contract` | Kiểm tra cấu trúc danh sách và tổng bản ghi API |
| `order_units`, `import_product_reference` | Quy đổi ĐVT DonDatHang và DonBanHang sang Thùng/Két/Bình; nhập bảng quy đổi từ danh mục sản phẩm |
| `region_mapping` | Gắn Vùng bán hàng cho báo cáo viếng thăm |
| `excel_export` | Ghi Excel nguyên tử và định dạng |
| `data_cham_anh_export` | Dựng workbook Data chấm ảnh từ monthly master |
| `report_runtime` | Đọc cờ môi trường, ghi manifest nguyên tử |
| `pipeline` | Điều phối, kiểm tra kết quả và ghi trạng thái từng bước |

## Lỗi và kiểm tra

Pipeline dừng khi bước nguồn lỗi, trả kết quả thất bại hoặc báo xuất thiếu file.
Các bước phụ thuộc được ghi `skipped`; các bước đã xuất thành công vẫn giữ trạng thái
để vận hành biết cần chạy lại phần nào. Việc ghi nhiều workbook không phải một
transaction toàn hệ thống; cơ chế staged publish và kiểm tra nội dung của từng bộ
xuất vẫn giữ nguyên.

Bốn báo cáo nguồn vẫn được thử độc lập trong `run_all_reports`: lỗi một báo cáo
không bỏ qua việc lấy các báo cáo nguồn còn lại, nhưng kết quả tổng thất bại sẽ
chặn bước Data chấm ảnh phía sau. Retry mạng có giới hạn; lỗi cấu trúc,
tổng bản ghi, khóa dữ liệu, giới hạn Excel hoặc nội dung publish không khớp đều
giữ nguyên cơ chế từ chối dữ liệu của bộ xuất nguồn.

Microsoft Graph `423 Locked` được retry có giới hạn, theo `Retry-After` nếu có hoặc
backoff hiện tại. Việc thay file vẫn giữ `If-Match` và rollback; không ép bỏ khóa.
Nếu file bị sửa trong lúc chờ thì `412` dừng cập nhật. Khóa kéo dài vẫn báo lỗi xuất
bản để người dùng đóng file rồi chạy lại, không đổi tên thành một báo cáo trùng.

`output/pipeline_manifest.json` ghi trạng thái/thời gian từng bước. Manifest của từng
bộ tiếp tục ghi nguồn, tháng, số dòng và kiểm chứng publish. GitHub chỉ lưu manifest
kiểm tra; workbook có dữ liệu khách không được upload dưới dạng Actions artifact.

## Build và kiểm tra

```powershell
python -m pip install -r requirements-dev.txt
python -m compileall -q src tests
python src/pipeline.py --check
ruff check .
coverage run -m unittest discover -s tests -v
coverage report
```

`--check` kiểm tra import và cấu hình bốn báo cáo, không cần token,
không gọi API và không ghi file. CI chạy các kiểm tra này trước khi merge.
Production chạy compile và preflight trước xác thực/tải dữ liệu; unit tests nằm trong CI.

## Lưu trữ và xác thực

Bốn báo cáo nguồn có một workbook chuẩn cho mỗi báo cáo/tháng. Cột ẩn `_sync_date`
giữ partition để thay dữ liệu của đúng ngày; không tạo lịch sử file ngày riêng.
Report phẳng dùng sheet `Data`; report đơn dùng `DonHang` (khóa `ma_phieu`) và
`ChiTietSP` (khóa `ma_phieu + stt`). Khi thiếu master của tháng, luồng nguồn rebuild
tháng theo các gate hiện có.

Token MobiWork và thông tin xác thực Microsoft chỉ được cấp lúc chạy. Repo không
chứa token, dữ liệu khách hoặc workbook xuất. Khi triển khai sang site khác,
cần cấu hình lại thư viện SharePoint và danh mục báo cáo đích.
