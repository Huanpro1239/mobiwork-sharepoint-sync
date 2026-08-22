# MobiWork SharePoint Sync

Production ETL pipeline for exporting MobiWork DMS reports to Excel and publishing them to SharePoint through Microsoft Graph.

```text
GitHub Actions
    -> production preflight
    -> MobiWork Open API
    -> monthly Excel master
    -> semantic workbook verification
    -> Microsoft Graph
    -> SharePoint MobiWorkDMS
```

## Production schedule

The production workflow is `.github/workflows/mobiwork-sync.yml` and uses `Asia/Ho_Chi_Minh`.

| Schedule | Scope | Purpose |
|---|---|---|
| `HH:05` every hour | `today` | Refresh current-day data so SharePoint stays near-real-time |
| `09:00` every day | `yesterday` | Finalize D-1 after the previous day is complete |

Both schedules use the same concurrency lock, so only one production job can write SharePoint at a time. GitHub-hosted runners may start a few minutes after the configured trigger.

## Storage model: one workbook per report/month

Each report/month has one canonical workbook:

```text
MobiWorkDMS/
├── 01_BaoCaoViengTham/YYYY/MM/BaoCaoViengTham_YYYY-MM.xlsx
├── 02_MoMoiKhachHang/YYYY/MM/MoMoiKhachHang_YYYY-MM.xlsx
├── 03_DonDatHang/YYYY/MM/DonDatHang_YYYY-MM.xlsx
├── 04_DonBanHang/YYYY/MM/DonBanHang_YYYY-MM.xlsx
└── _sync_runs/YYYY/MM/*.json
```

The hidden `_sync_date` column identifies the daily partition inside a monthly workbook. An hourly `today` run replaces only today's partition. The 09:00 run replaces only yesterday's partition. The canonical monthly filename does not change, so hourly execution does not create duplicate business workbooks.

When a monthly master does not yet exist, production rebuilds that report from the first day of the month through the target date. Only after the new master uploads and passes semantic verification does cleanup remove legacy files for that report/month:

- `Report_YYYY-MM-DD.xlsx`
- `Report_History_*.xlsx`
- orphan `__sync_tmp_*`, `__sync_backup_*`, and `__sync_failed_*` files

Unrelated files are never removed by this cleanup matcher.

## Incremental scopes

`src/run_all_reports.py` is the only runtime sync entry point and supports:

- `today`: current Vietnam calendar day; automatic hourly scope.
- `yesterday`: previous Vietnam calendar day; automatic 09:00 scope.
- `lookback`: previous N days; manual recovery/backfill, limited to 31 days.

Manual workflow dispatch defaults to `today`. There is no legacy bootstrap/history-file runtime path, so normal or manual execution cannot intentionally generate `History_*.xlsx` again.

## Enabled reports

Configuration lives in `config/reports.json`.

| Key | MobiWork API | SharePoint folder | Export |
|---|---|---|---|
| `visit` | `/OpenAPI/V1/VisitData` | `01_BaoCaoViengTham` | flat rows |
| `new_customer` | `/OpenAPI/V1/Customer` | `02_MoMoiKhachHang` | flat rows |
| `order` | `/OpenAPI/V1/Order` | `03_DonDatHang` | `DonHang` + `ChiTietSP` |
| `bill` | `/OpenAPI/V1/Bill` | `04_DonBanHang` | `DonHang` + `ChiTietSP` |

Reports execute independently. If one report fails, the remaining reports are still attempted. The overall workflow finishes as `partial_failure`/failed when any report fails, so incomplete data is visible rather than silently accepted.

## Order / bill data contract

MobiWork line items are normalized into one `ChiTietSP` table. Sold and promotional products are distinguished by `is_km` and `loai_hang`.

Business keys:

```text
DonHang   -> ma_phieu
ChiTietSP -> ma_phieu + stt
```

Historical MobiWork data does not always provide `stt`. The exporter preserves every valid source `stt` and assigns only missing/invalid line numbers to the first unused positive integer in source order. Duplicate valid `stt` values from MobiWork are still rejected; the fallback never hides a real duplicate-key defect.

Business codes such as `ma_sp = "00008"` remain text. Numeric values are normalized and UTC timestamps are converted to `Asia/Ho_Chi_Minh` before Excel export.

## Excel integrity model

SharePoint/Office may repack an `.xlsx` OOXML ZIP package after upload, changing physical byte size or SHA-256 while worksheet data remains unchanged. Excel integrity is therefore verified semantically.

`SemanticSharePointClient` downloads the uploaded workbook and compares:

- worksheet order and names;
- every non-empty cell coordinate;
- cell data type;
- cell value.

Any business-cell difference fails closed. JSON/audit files continue to use normal byte/size integrity behavior.

## Reliability model

- MobiWork requests use throttling, timeout, retry/backoff, jitter, and `Retry-After` handling.
- Required fields, API totals where configured, and business keys are validated.
- Microsoft Graph authentication refreshes rejected/expiring tokens.
- Graph retries transient network errors, `429`, and `5xx` responses.
- Existing Excel files use staged replacement with rollback protection.
- Excel uploads are semantically verified before legacy cleanup.
- Every production run writes `output/sync_manifest.json` and uploads an audit JSON to `_sync_runs`.
- `source_row_count` means rows fetched for the target date(s) in the current run; `master_row_count` means total rows stored across the monthly masters written by that run.

## CI quality gate

Pull requests and pushes to `main` run:

1. Python compilation
2. Ruff static analysis
3. Unit tests
4. branch-aware coverage with the configured minimum threshold

Production hourly runs use a lighter compile/config preflight to keep latency low; full tests remain a change-control gate in CI.

## Manual recovery

Manual runs use the same monthly-master implementation as automatic production. Select `today`, `yesterday`, or `lookback`; use `dry_run=true` to inspect generated Excel without SharePoint writes.

For corrections older than 31 days, extend the monthly-master backfill capability through reviewed code rather than reintroducing legacy history-file exports.

## Security

Required GitHub Actions secrets:

```text
MOBIWORK_USER
MOBIWORK_TOKEN
AZURE_CLIENT_ID
AZURE_TENANT_ID
```

`MOBIWORK_USER_ID` remains accepted as a backward-compatible alternative to `MOBIWORK_USER`.

Microsoft authentication uses GitHub OIDC -> Microsoft Entra -> Azure CLI/AzureCliCredential. Do not commit `.env`, access tokens, passwords, Authorization headers, generated business Excel files, or exported business data.

## Operations

See [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for the production runbook and failure-recovery procedure.
