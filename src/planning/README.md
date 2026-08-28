# Cấu trúc `src/planning`

Thư mục này là **đầu não tính kế hoạch Vikoda**. Quy tắc tổ chức là: mỗi module chỉ nên chịu trách nhiệm cho một lớp nghiệp vụ, còn `engine.py` chỉ điều phối luồng chạy.

## Sơ đồ module

```text
src/planning/
├── config.py              # Đọc cấu hình nguồn SharePoint
├── engine.py              # Orchestrator: tải nguồn -> gọi nghiệp vụ -> xuất kết quả
├── excel_io.py            # Đọc/ghi workbook, không chứa business rule
├── normalize.py           # Chuẩn hóa mã, số, text tiếng Việt
├── source_refresh.py      # Adapter đọc sheet/header cho các nguồn Excel
├── vba_port.py            # Port 9 bước VBA Call_All / stock reconciliation
├── formula_port.py        # Compatibility facade; KHÔNG viết logic mới ở đây
├── rgb_scheduler.py       # Scheduler riêng cho dây chuyền RGB
└── domain/
    ├── common.py          # Helper ngày, MOQ, ROUNDUP kiểu Excel
    ├── demand.py          # Forecast và projection thành phẩm
    ├── materials.py       # BOM, nhu cầu NVL, PO, phân bổ NVL ngày
    ├── purchasing.py      # ABC, shortage, MOQ, lead time, đề xuất mua
    └── production.py      # KHSX tuần và scheduler KHS/PET/Galon
```

## Quy tắc đặt code

1. **Không thêm business rule mới vào `engine.py`.** Engine chỉ nối các bước và chuẩn bị dữ liệu.
2. **Không thêm business rule mới vào `formula_port.py`.** File này chỉ giữ API cũ để tránh phá import hiện tại.
3. Quy tắc forecast/tồn thành phẩm đặt trong `domain/demand.py`.
4. Quy tắc BOM/NVL/PO đặt trong `domain/materials.py`.
5. Quy tắc ABC/mua hàng/MOQ/lead time đặt trong `domain/purchasing.py`.
6. Quy tắc lịch KHS/PET/Galon đặt trong `domain/production.py`; RGB đặt trong `rgb_scheduler.py` cho tới khi hai scheduler được hợp nhất có kiểm soát.
7. Module domain không được gọi SharePoint/Graph trực tiếp. I/O bên ngoài chỉ đi qua `engine.py` và client SharePoint.
8. Đường dẫn SharePoint không hard-code trong business rule; dùng `config/planning_sources.json`.
9. Mỗi thay đổi business rule phải có unit/regression test trước khi merge `main`.
10. Workbook `.xlsm` hiện là nguồn đối chiếu/rollback; Python là engine shadow, chưa được phép ghi đè workbook gốc.

## API tương thích

Các import cũ như sau vẫn được hỗ trợ:

```python
from src.planning.formula_port import build_purchase_plan
```

Nhưng code mới nên import trực tiếp theo domain:

```python
from src.planning.domain.purchasing import build_purchase_plan
```

Cách này cho phép dọn cấu trúc dần mà không gây gián đoạn workflow production.
