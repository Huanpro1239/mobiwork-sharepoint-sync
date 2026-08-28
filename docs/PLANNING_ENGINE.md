# Vikoda Planning Engine — kiến trúc hiện tại

## Trạng thái

Planning Engine hiện chạy **production-shadow V2** trên GitHub Actions và SharePoint thật.

Đã tự động hóa:

- 9/9 bước refresh dữ liệu tương đương `Call_All` VBA;
- forecast/tồn/nợ kho và `Tinh ung hang`;
- BOM/MRP;
- kế hoạch nhập NVL;
- ABC;
- kế hoạch mua theo MOQ/lead time/ngày thiếu;
- kế hoạch sản xuất tuần;
- lịch sản xuất ngày KHS/PET 9000/Galon/RGB;
- phân bổ NVL theo lịch sản xuất ngày.

Workbook `.xlsm` production hiện **không bị ghi đè**. Python publish kết quả vào thư mục shadow để đối chiếu và làm rollback an toàn.

## Kiến trúc

```text
SharePoint source workbooks
        |
        v
Microsoft Graph / SharePointClient
        |
        v
source_refresh.py + vba_port.py
        |
        v
normalize.py
        |
        v
planning/domain/
  demand.py
  materials.py
  purchasing.py
  production.py
        |
        +--> rgb_scheduler.py
        |
        v
engine.py (orchestration only)
        |
        v
excel_io.py
        |
        v
planning_shadow.xlsx + planning_manifest.json
        |
        v
SemanticSharePointClient staged replacement
        |
        v
SharePoint _PlanningEngine/shadow
```

## Boundary từng module

| Module | Trách nhiệm |
|---|---|
| `config.py` | đọc source contract/đường dẫn |
| `source_refresh.py` | đọc sheet/header/range theo cấu trúc nguồn |
| `vba_port.py` | logic refresh tương đương VBA |
| `normalize.py` | chuẩn hóa mã/số/text/quy đổi |
| `domain/demand.py` | forecast và projection thành phẩm |
| `domain/materials.py` | BOM, MRP, PO, phân bổ NVL ngày |
| `domain/purchasing.py` | ABC, shortage, MOQ, lead time, mua hàng |
| `domain/production.py` | KHSX tuần, KHS/PET/Galon scheduler |
| `rgb_scheduler.py` | scheduler dây chuyền RGB |
| `formula_port.py` | compatibility facade, không chứa logic mới |
| `engine.py` | orchestration: download -> calculate -> output |
| `excel_io.py` | Excel input/output |

## Vì sao vẫn giữ shadow mode

Mục tiêu không còn là “port code cho chạy được”, mà là **chứng minh parity và độ ổn định vận hành** trước khi Python trở thành nguồn canonical.

Shadow mode cho phép:

- dùng dữ liệu production thật;
- chạy tự động theo lịch;
- không phụ thuộc Excel Desktop;
- đối chiếu kết quả Python với workbook hiện tại;
- rollback tức thì vì file gốc chưa bị thay đổi.

## Quality gate

Mỗi thay đổi code phải đi qua:

```text
compile Python
    -> Ruff
    -> unit/regression tests
    -> coverage gate
    -> merge main
    -> production-shadow run
    -> manifest + SharePoint verification
```

Business rules quan trọng có regression tests cho:

- `Tinh ung hang` semantics;
- demand theo Flat BOM;
- ngày thiếu;
- ABC;
- MOQ/lead time;
- ngày mua/đặt mua;
- kế hoạch tuần;
- Galon;
- dynamic date columns;
- RGB capacity/no-overlap.

## Automation

`.github/workflows/planning-engine.yml` chạy giờ Việt Nam, Thứ Hai-Thứ Bảy:

- 07:10
- 11:10
- 14:10
- 17:10

Ngoài ra workflow chạy khi source planning trên `main` thay đổi và hỗ trợ manual `workflow_dispatch` với `dry_run`.

GitHub OIDC xác thực Microsoft Entra; không dùng Microsoft client secret trong workflow.

## Output contract

SharePoint folder:

```text
Tinh san xuat Mua hang 2027/_PlanningEngine/shadow/
```

Canonical shadow files:

```text
planning_shadow.xlsx
planning_manifest.json
```

Workbook shadow chứa các bảng từ source reconciliation đến mua hàng, KHSX tuần/ngày và phân bổ NVL.

## Cutover gate

Chỉ chuyển Python thành nguồn canonical sau khi:

1. source refresh ổn định;
2. MRP/mua hàng đạt parity được nghiệp vụ xác nhận;
3. scheduler hỗ trợ toàn bộ mã cần sản xuất;
4. `unsupported_weekly_rows = 0` ổn định;
5. không có unexplained variance trong chuỗi run production; khuyến nghị 10 run liên tiếp;
6. alert/audit/rollback đã rõ;
7. người dùng nghiệp vụ chấp thuận output Python là nguồn chuẩn.

Sau cutover:

```text
Python/GitHub = calculation brain
SharePoint    = source + result hub
Excel         = reporting/configuration UI
VBA           = historical rollback only
```

Quy trình vận hành chi tiết xem [`PLANNING_PROCESS.md`](PLANNING_PROCESS.md).
