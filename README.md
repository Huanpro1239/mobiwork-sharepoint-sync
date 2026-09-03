# MobiWork DMS → SharePoint

Pipeline Python tự động đồng bộ báo cáo và ảnh gốc từ MobiWork DMS sang thư viện tài liệu SharePoint `MobiWorkDMS`.

```text
MobiWork Open API
   ├─ báo cáo ──► monthly master trên SharePoint
   └─ ảnh ──────► Data anh/YYYY-MM/<nhân viên>/<khách hàng>/...
```

Dự án không chấm điểm ảnh và không tạo KPI. Ảnh chỉ được tải, kiểm tra định dạng và lưu để sử dụng như dữ liệu gốc.

## Chức năng chính

- Đồng bộ các báo cáo được bật trong `config/reports.yaml` theo ngày.
- Gộp dữ liệu vào một workbook chuẩn cho mỗi báo cáo/tháng.
- Thay thế file SharePoint theo cơ chế staged upload, kiểm tra nội dung và rollback khi cần.
- Tránh ghi lại workbook khi nội dung nghiệp vụ không đổi.
- Đọc liên kết ảnh từ monthly master viếng thăm đã lưu trên SharePoint.
- Tải ảnh theo lô, bỏ qua ảnh đã có và tiếp tục từ checkpoint nếu chưa xong.
- Chỉ giữ thư mục ảnh của tháng hiện tại và tháng trước.
- Theo dõi sức khỏe và tự kiểm tra tính nhất quán của dữ liệu production.

## Chạy cục bộ

```powershell
python -m pip install -r requirements.txt
python src\run_all_reports.py
python src\run_images.py
```

Sao chép `.env.example` thành `.env` và điền thông tin MobiWork/SharePoint trước khi chạy. Không commit file `.env`, token, dữ liệu khách hàng, ảnh hoặc file xuất ra.

## Tự động hóa

- `.github/workflows/mobiwork-sync.yml`: cập nhật báo cáo theo giờ; lượt 09:00 chốt D-1 rồi gọi đồng bộ ảnh.
- `.github/workflows/mobiwork-images.yml`: tải ảnh theo lô và tự chạy tiếp khi còn backlog.
- `.github/workflows/nightly-reconcile.yml`: đối soát lại ba ngày gần nhất mỗi đêm.
- `.github/workflows/production-smoke.yml`: kiểm tra báo cáo và ảnh thực tế, có một lần tự phục hồi có giới hạn.
- `.github/workflows/operations-health.yml`: giám sát độ mới và trạng thái các workflow production.
- `.github/workflows/ci.yml`: compile, lint và unit test.

## Kiểm tra

```powershell
python -m pip install -r requirements-dev.txt
python -m compileall -q src tests
ruff check .
coverage run -m unittest discover -s tests -v
coverage report
```

Xem [runbook vận hành](docs/OPERATIONS.md), [đồng bộ ảnh](docs/image-sync.md) và [bảo mật](SECURITY.md) để biết thêm chi tiết.
