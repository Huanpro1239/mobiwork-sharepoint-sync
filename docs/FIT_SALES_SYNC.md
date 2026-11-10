# FIT ĐH Bán trong kỳ NPP → SharePoint (lệnh Telegram /banhang)

Chung hạ tầng với Nhận hàng: Worker `erp-nhan-hang` giữ phiên FIT (kiểm tra 15 phút/lần, hết hạn thì gửi
CAPTCHA qua Telegram `/dangnhap`), cùng service token Cloudflare Access và cùng quyền SharePoint Planning.

## Luồng

1. Telegram `/banhang [kỳ]` → Worker gửi `repository_dispatch` `fit_ban_hang` (start, end) tới repo này.
2. Workflow `fit-sales-sync.yml` chạy `src/fit_sales_sync.py`: gọi
   `GET /api/integrations/ban-hang-npp/file?start=&end=&muc=don_hang|hoa_don` (service token chỉ được đọc đúng route này).
3. Lấy đủ 2 file rồi mới ghi đè lên SharePoint (thư viện Tài liệu, thư mục `DH ban trong ky NPP` – đổi bằng
   repository variable `FIT_SALES_SP_FOLDER`):

| Mức lấy | File |
|---|---|
| Đơn hàng | `BCDonHangBanTrongKyNPP.xlsm` |
| Hóa Đơn | `Lay So Hoa don.xlsm` |

4. Gửi 2 file + link vào Telegram. GitHub chỉ **gửi** tin, không `getUpdates` (Worker là nơi duy nhất đọc tin của bot).

Bộ lọc FIT: Từ/Đến ngày, Xem thời gian = Ngày hóa đơn, Không pivot, Lấy chi nhánh tắt, các ô khác All.
Lịch 07:05 VN (đầu tháng→hôm nay) và chạy tay (ô `ky`: trống | thangtruoc | 10/2026 | 01/10/2026 15/10/2026).

## Secrets

Có sẵn: `FIT_CF_ACCESS_CLIENT_ID`, `FIT_CF_ACCESS_CLIENT_SECRET`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`.
Thêm: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Worker cần secret `GITHUB_DISPATCH_TOKEN`
(fine-grained, chỉ repo này, Contents: Read and write) và biến `GITHUB_REPO=Huanpro1239/mobiwork-sharepoint-sync`.

## Chẩn đoán HTTP 502 an toàn (không thay đổi báo cáo production)

Vào Actions → **FIT Ban Hang NPP to SharePoint** → Run workflow.
Chọn nhánh chứa thay đổi (sau merge chọn `main`), nhập `ky=08/11/2026`
và bật **probe_only**. Probe chỉ yêu cầu xuất báo cáo **Đơn hàng** trong một ngày;
không ghi SharePoint, không tạo artifact và không gửi Telegram.

- Probe vẫn HTTP 502: lỗi ở Worker/phiên FIT hoặc bước xuất báo cáo phía FIT; phải
  kiểm tra log Worker tại route `/api/integrations/ban-hang-npp/file` (muc=don_hang).
- Probe thành công nhưng chạy kỳ tháng lỗi: điều tra thời gian xuất/khối lượng
  dữ liệu ở Worker. Không tự ghép nhiều workbook vì có thể mất macro, định dạng
  hoặc sai nghiệp vụ.
- Sau khi Worker được sửa, chạy lại kỳ tháng chuẩn và xác minh **cả hai file**
  cùng được cập nhật thành công trên SharePoint.

Không bật probe_only trên lịch; đây là bước kiểm tra thủ công.
