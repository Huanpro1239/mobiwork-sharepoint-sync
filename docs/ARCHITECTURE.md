# Architecture

## Purpose

MobiWork SharePoint Sync is a small Python ETL pipeline that reads MobiWork DMS Open API reports, normalizes them into analytics-friendly Excel workbooks, and publishes one canonical workbook per report/month to SharePoint through Microsoft Graph.

## Data flow

```text
GitHub Actions / local runner
        |
        v
MobiWorkClient
  - Basic Auth
  - throttling/retry/backoff
  - pagination and API-total validation
        |
        v
normalization / validation
  - required fields
  - business keys
  - code/date/numeric normalization
  - order + line-item shaping
        |
        v
monthly_master
  - hidden _sync_date partition key
  - replace target day only
  - rebuild current month when master is missing
        |
        v
Excel writer
  - flat Data sheet, or DonHang + ChiTietSP
        |
        v
SemanticSharePointClient
  - Microsoft Graph
  - staged replacement for existing files
  - semantic workbook verification
  - rollback on promotion failure
        |
        v
SharePoint monthly master + _sync_runs audit JSON
```

## Module map

| Module | Responsibility |
|---|---|
| `src/mobiwork.py` | API client, throttling, retries, pagination, response validation |
| `src/excel_export.py` | data normalization and Excel formatting helpers |
| `src/monthly_master.py` | monthly partition merge/rebuild and canonical workbook writing |
| `src/sharepoint.py` | Microsoft Graph authentication, folders, upload/download, staged replacement |
| `src/sharepoint_semantic.py` | Excel-specific semantic verification after SharePoint upload |
| `src/main.py` | shared configuration, manifest and audit helpers |
| `src/run_all_reports.py` | production orchestration and report isolation |

## Storage contract

The business storage model is intentionally simple: one workbook per report/month. `_sync_date` is stored as a hidden worksheet column so a single day can be replaced deterministically without creating daily-history files.

For order-style reports:

```text
DonHang   -> primary business key: ma_phieu
ChiTietSP -> primary business key: ma_phieu + stt
```

For flat reports, source rows are normalized into one `Data` sheet.

## Failure model

Deterministic integrity failures are fail-closed: malformed API data, incomplete configured totals, missing business keys, duplicate keys, Excel row-limit violations, upload/promotion failures, and semantic workbook mismatches fail the affected report.

Transient HTTP/network conditions are retried with bounded backoff. Reports execute independently so one report failure does not suppress attempts for the others; the overall workflow still exits non-zero when any report fails.

## Trust boundaries

Credentials are supplied at runtime. Repository code should contain no MobiWork password/token, Microsoft access token, customer export, or generated workbook. The SharePoint target and MobiWork report mappings are deployment configuration and should be reviewed before publishing a fork.
