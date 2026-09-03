# Tối ưu vận hành production

Pipeline MobiWork → SharePoint áp dụng ba tối ưu chính:

1. Workbook không đổi về nội dung nghiệp vụ sẽ không được upload lại.
2. Lookback nhiều ngày được gộp theo báo cáo/tháng, nên mỗi monthly master chỉ cần tải và publish tối đa một lần trong một batch.
3. Đồng bộ ảnh dùng folder index, đường dẫn xác định, checkpoint và giới hạn batch để giảm Graph API call và tiếp tục an toàn sau khi hết thời gian chạy.

Nightly reconciliation chạy lúc 23:30 theo giờ Việt Nam, đối soát D-1 đến D-3 rồi gọi image reconciliation từ ngày sớm nhất. Operations health chạy mỗi hai giờ và theo dõi report sync, image sync cùng production smoke.

Các chỉ số nên theo dõi hàng tuần:

| Chỉ số | Mục tiêu |
|---|---:|
| Scheduled report success rate | >= 99% |
| Daily image reconciliation success | >= 99% |
| Stale pipeline incident | 0 kéo dài > 4 giờ |
| Manual backfill D-1..D-3 | gần 0 |
| SharePoint writes / target executions | giảm theo batch/no-op |

`xlsx_semantic_noop` cao là tín hiệu tốt: pipeline vẫn kiểm tra thường xuyên nhưng không ghi SharePoint khi dữ liệu không đổi.
