# Tối ưu vận hành production

Pipeline MobiWork → SharePoint áp dụng ba tối ưu chính:

1. Workbook không đổi về nội dung nghiệp vụ sẽ không được upload lại.
2. Lookback nhiều ngày được gộp theo báo cáo/tháng, nên mỗi monthly master chỉ cần tải và publish tối đa một lần trong một batch.
3. Đồng bộ ảnh dùng folder index, đường dẫn xác định, checkpoint và giới hạn batch để giảm Graph API call và tiếp tục an toàn sau khi hết thời gian chạy.

Nightly reconciliation chạy lúc 23:30 theo giờ Việt Nam, đối soát D-1 đến D-3. Image sync hiện chỉ chạy thủ công. Operations health chạy lúc 08:20 hằng ngày và theo dõi report sync, full-month rebuild cùng production smoke.

Các chỉ số nên theo dõi hàng tuần:

| Chỉ số | Mục tiêu |
|---|---:|
| Scheduled report success rate | >= 99% |
| Daily image reconciliation success | >= 99% |
| Stale pipeline incident | 0 kéo dài > 4 giờ |
| Manual backfill D-1..D-3 | gần 0 |
| SharePoint writes / target executions | giảm theo batch/no-op |

`xlsx_semantic_noop` cao là tín hiệu tốt: pipeline vẫn kiểm tra thường xuyên nhưng không ghi SharePoint khi dữ liệu không đổi.

## Lịch chạy tiết kiệm phút GitHub Actions (từ 2026-09)

| Workflow | Lịch | Ghi chú |
|---|---|---|
| mobiwork-sync (today) | 07:05, 10:05, 13:05, 16:05, 19:05 — Thứ 2–7 | Bỏ rebuild Data cham anh nếu không có master nào thay đổi |
| mobiwork-sync (yesterday) | 09:00 hằng ngày | Luôn rebuild Data cham anh |
| nightly-reconcile | 23:30 hằng ngày | Lookback D-1..D-3 |
| production-smoke | 11:30 hằng ngày | Không kiểm tra ảnh; timeout 90 phút |
| operations-health | 08:20 hằng ngày | Ngưỡng stale 12 giờ; không theo dõi ảnh |
| data-cham-anh-backfill | 10:15 Thứ 2 | Tự sửa tháng trước + tháng hiện tại |
| recovery-rebuild | CN 02:00 (tháng hiện tại), ngày 2 lúc 03:30 (tháng trước) | |
| mobiwork-images | Chỉ chạy tay | Data cham anh dùng link ảnh gốc MobiWork (`hinh_anh`) |
| historical-reconcile | Chỉ chạy tay | Quét lại toàn bộ lịch sử khi cần |
| Dependabot | Hàng tháng, gộp 1 PR | |

Artifact giữ 7 ngày.

Preflight production chỉ `compileall` + kiểm tra config; unit test chạy trong CI. CI bỏ qua thay đổi chỉ ở `docs/` và `*.md`.
