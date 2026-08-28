# Vikoda Planning Engine

Planning Engine đã được tích hợp vào repository này và đang chạy ở chế độ **production-shadow V2**.

Hệ thống hiện tự động:

- đọc dữ liệu kế hoạch từ SharePoint qua Microsoft Graph;
- thay 9 bước refresh của VBA `Call_All`;
- tính forecast/tồn/nợ kho;
- tính BOM/MRP và kế hoạch nhập NVL;
- phân tích ABC và đề xuất mua theo MOQ/lead time;
- tính kế hoạch sản xuất tuần;
- tự xếp kế hoạch ngày cho KHS, PET 9000, Galon và RGB;
- phân bổ nhu cầu NVL theo lịch sản xuất ngày;
- publish `planning_shadow.xlsx` và `planning_manifest.json` lên SharePoint.

File `.xlsm` gốc hiện vẫn được giữ nguyên để đối chiếu/rollback và chưa bị Python ghi đè.

## Tài liệu chính

- Quy trình vận hành end-to-end: [`docs/PLANNING_PROCESS.md`](docs/PLANNING_PROCESS.md)
- Kiến trúc Planning Engine: [`docs/PLANNING_ENGINE.md`](docs/PLANNING_ENGINE.md)
- Mapping VBA -> Python: [`docs/VBA_MIGRATION_MAP.md`](docs/VBA_MIGRATION_MAP.md)
- Cấu trúc source planning: [`src/planning/README.md`](src/planning/README.md)

## Entry point

```bash
python src/run_planning_engine.py
```

Workflow production:

```text
.github/workflows/planning-engine.yml
```
