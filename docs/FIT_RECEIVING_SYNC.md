# FIT Nhận hàng → SharePoint

Workflow cập nhật một file Excel Nhận hàng cho tháng hiện tại mỗi ngày. Lịch tự động chỉ chạy khi repository variable `FIT_RECEIVING_SYNC_ENABLED=true`; workflow vẫn hỗ trợ chạy thủ công để nghiệm thu trước khi bật lịch.

## Kết quả đã chốt

- Chạy 07:00 mỗi ngày, giờ Việt Nam (`00:00 UTC`). GitHub có thể xếp lịch chạy trễ khi tải cao.
- Lấy đầy đủ Nhận hàng từ đầu tháng đến ngày chạy, cùng 5 sheet như website.
- Ghi vào site Planning, thư viện URL `Shared Documents`, thư mục `Nhan hang` đã xác minh. Drive ID được cố định trong runner để tránh chọn nhầm thư viện MobiWorkDMS.
- Tên chuẩn `Nhan_hang_YYYY_MM.xlsx`, cập nhật cùng file trong tháng; sang tháng tạo tên mới.
- Nguồn lỗi, hết phiên FIT, hết thời gian chạy, Excel thiếu sheet hoặc sai lượt xuất: không cập nhật file SharePoint. Thay thế file thành công được kiểm tra bằng bộ kiểm tra nội dung Excel đang dùng cho MobiWork.
- Dùng OIDC Microsoft hiện có trong repository, không cần lưu mật khẩu Microsoft. File nghiệp vụ chỉ tạm ở runner khi xử lý; không thêm Actions artifacts.

## Quyền máy cần duyệt trước khi kích hoạt

1. Tạo Cloudflare Access service token tên `fit-receiving-daily`, hết hạn sau một năm; thêm Service Auth policy chỉ cho token này trên ứng dụng ERP hiện có, giữ chính sách người dùng hiện tại.
2. Worker chỉ chấp nhận Client ID được khai báo rõ qua `ERP_SYNC_CLIENT_ID`, sau khi xác minh chữ ký JWT, issuer và audience. Không cấu hình ID thì không có quyền máy.
3. Token chỉ dùng được `POST /api/integrations/receiving` (xuất tháng hiện tại), `GET /api/integrations/receiving/{id}` (trạng thái) và `.../{id}/file` (file thuộc lượt đó). Không dùng được đăng nhập, đọc phiên/cookie/mật khẩu, API quản trị, xóa file hoặc xuất báo cáo khác.
4. Lưu Client ID/Secret trong GitHub Actions secrets `FIT_CF_ACCESS_CLIENT_ID`, `FIT_CF_ACCESS_CLIENT_SECRET` của `Huanpro1239/mobiwork-sharepoint-sync`. Không lưu giá trị trong mã nguồn, log hay tài liệu.
5. Triển khai Worker đã sửa, đưa workflow vào default branch, chạy thủ công để kiểm tra quyền ứng dụng Microsoft trên đúng thư mục và xác minh file sau upload. Chỉ sau khi thành công mới đặt repository variable `FIT_RECEIVING_SYNC_ENABLED=true` để bật lịch.

Không bật lịch trước khi kiểm tra thành công toàn bộ luồng bằng danh tính máy. Không đưa giá trị token vào mã nguồn hoặc log.

## Giới hạn xác thực FIT

Worker có kiểm tra giữ phiên mỗi 15 phút khi `ERP_SYNC_KEEPALIVE_ENABLED=true`. Kiểm tra chỉ đọc màn hình Nhận hàng, không khởi động container Excel; bỏ qua khi đang xuất/đăng nhập hoặc có lượt đọc FIT khác. Cookie được máy chủ xoay vòng sẽ được lưu mã hóa lại, không làm thay đổi trạng thái xuất hoặc file đã hoàn tất.

Lịch tự dùng phiên FIT đã lưu. Nếu FIT yêu cầu CAPTCHA/đăng nhập mới, runner dừng với `needs_login` và giữ file SharePoint cũ. Không thể cam kết tự đăng nhập vĩnh viễn qua CAPTCHA. Muốn bỏ hoàn toàn thao tác xác nhận FIT cần tài khoản/API tích hợp do FIT hỗ trợ.

## Các phần mã

- Workflow: `.github/workflows/fit-receiving-sync.yml`.
- Runner: `src/fit_receiving_sync.py`; kiểm thử `tests/test_fit_receiving_sync.py`.
- Worker ở dự án ERP cloud: `worker/src/receiving-sync.js`, giới hạn danh tính trong `worker/src/security.js`, định tuyến trong `worker/src/index.js`.

Đã đạt Ruff/compile và 227 kiểm thử repository trên main mới nhất (coverage 69%), gồm 8 kiểm thử runner mới. Worker đạt 160 kiểm thử Node và đóng gói dry run. Nghiệm thu live cần xác minh danh tính máy, quyền ghi đúng thư mục và tải lại kiểm tra nội dung Excel trước khi bật lịch.
