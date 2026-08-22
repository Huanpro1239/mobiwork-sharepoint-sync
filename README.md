# MobiWork SharePoint Sync

Production ETL pipeline for exporting MobiWork DMS reports to Excel and publishing them to SharePoint through Microsoft Graph.

```text
GitHub Actions
    -> quality gate
    -> MobiWork Open API
    -> normalized Excel workbooks
    -> semantic workbook verification
    -> Microsoft Graph
    -> SharePoint MobiWorkDMS
```

## Production schedule

The production workflow is `.github/workflows/mobiwork-sync.yml`.

- Schedule: **09:00 every day**
- Timezone: `Asia/Ho_Chi_Minh`
- Scheduled mode: `incremental`
- Scheduled lookback: **1 day (D-1)**
- Scheduled dry run: `false`
- Concurrency: only one production sync may write at a time

Example: the run on `2026-08-22` refreshes the files for `2026-08-21`.

If MobiWork data for older dates is corrected later, use a manual workflow run and increase `lookback_days` only for the required recovery window.

## Production target

- SharePoint host: `vikodacomvn.sharepoint.com`
- Site: `/sites/Planning`
- Document library: `MobiWorkDMS`

## Enabled reports

Configuration lives in `config/reports.json`.

| Key | MobiWork API | SharePoint folder | Export |
|---|---|---|---|
| `visit` | `/OpenAPI/V1/VisitData` | `01_BaoCaoViengTham` | flat visit rows |
| `new_customer` | `/OpenAPI/V1/Customer` | `02_MoMoiKhachHang` | flat rows |
| `order` | `/OpenAPI/V1/Order` | `03_DonDatHang` | header + line items |
| `bill` | `/OpenAPI/V1/Bill` | `04_DonBanHang` | header + line items |

Daily incremental execution uses `src/run_all_reports.py`. Every report is isolated: a failure in one report is recorded but does not prevent the remaining reports from being attempted. The workflow still finishes as failed/partial-failure when any report fails, so incomplete runs are visible.

## Excel integrity model

SharePoint/Office can repack an `.xlsx` OOXML ZIP package after upload, changing its physical byte size or SHA-256 without changing worksheet data. Therefore production does **not** require byte-for-byte equality for Excel files.

`SemanticSharePointClient` downloads the uploaded workbook and verifies:

- worksheet order and names;
- every non-empty cell coordinate;
- cell data type;
- cell value.

If any business cell differs, the upload fails closed. JSON/audit files continue to use normal byte/size integrity behavior.

The production validation on 2026-08-22 successfully updated all four reports with semantic verification.

## Sales-order / bill model

MobiWork returns order lines in `san_pham`. Sold and promotional items remain in one analytical detail table and are distinguished by `is_km`.

Each order/bill workbook contains:

```text
DonHang
ChiTietSP
```

- `DonHang` business key: `ma_phieu`
- `ChiTietSP` business key: `ma_phieu + stt`
- `is_km=false` -> `Bán hàng`
- `is_km=true` -> `Khuyến mãi`

The exporter preserves business codes such as `ma_sp = "00008"` as text, normalizes numeric fields, converts UTC timestamps to `Asia/Ho_Chi_Minh`, and rejects duplicate business keys.

## Reliability model

The pipeline is designed to fail visibly rather than silently publish incomplete data.

- MobiWork requests use throttling, timeouts, retry/backoff, jitter, and `Retry-After` handling.
- Configured required fields and business keys are validated before export.
- API totals are verified where configured.
- Microsoft Graph requests refresh rejected/expiring tokens and retry transient network, `429`, and `5xx` failures.
- Existing Excel files are replaced through staged upload/promotion with rollback protection.
- Excel uploads are semantically verified after SharePoint processing.
- Daily reports run independently so one report does not block the remaining reports.
- Every run writes `output/sync_manifest.json` and uploads a production audit manifest to SharePoint `_sync_runs`.
- GitHub Actions keeps a short-lived copy of the manifest as an artifact.

## SharePoint layout

```text
MobiWorkDMS/
├── 01_BaoCaoViengTham/YYYY/MM/*.xlsx
├── 02_MoMoiKhachHang/YYYY/MM/*.xlsx
├── 03_DonDatHang/YYYY/MM/*.xlsx
├── 04_DonBanHang/YYYY/MM/*.xlsx
├── _sync_runs/YYYY/MM/*.json
└── _sync_state/bootstrap.json
```

`_sync_runs` is the audit trail. `_sync_state/bootstrap.json` is used only by resumable historical bootstrap runs.

## Run modes

### Incremental

Normal production mode. The scheduled workflow refreshes D-1 only.

Manual workflow runs may set a larger `lookback_days` when a known older date must be refreshed.

### Bootstrap

Bootstrap is a manual historical mode. It walks backward month by month and checkpoints progress to `_sync_state/bootstrap.json`, allowing an interrupted history load to resume.

Use `reset_bootstrap_state=true` only when intentionally restarting the historical scan.

### Dry run

A manual dry run fetches MobiWork data and generates Excel files without Microsoft authentication or SharePoint writes. GitHub Actions publishes those workbooks as short-lived artifacts.

## Security

Required GitHub Actions secrets:

```text
MOBIWORK_USER
MOBIWORK_TOKEN
AZURE_CLIENT_ID
AZURE_TENANT_ID
```

`MOBIWORK_USER_ID` is accepted as a backward-compatible alternative to `MOBIWORK_USER`.

Microsoft authentication uses GitHub OIDC -> Microsoft Entra -> Azure CLI/AzureCliCredential. No Microsoft client secret is stored in the repository. Use least-privilege Graph access, preferably `Sites.Selected` limited to the Planning site.

Never commit `.env`, access tokens, passwords, Authorization headers, generated Excel files, or exported business data.

## CI quality gate

Pull requests and pushes to `main` run:

1. Python compilation
2. Ruff static analysis
3. Unit tests
4. Branch-aware coverage with a minimum threshold

Tests include MobiWork pagination/data-integrity checks, Excel normalization, independent all-report execution, Graph authentication behavior, staged replacement/rollback, and semantic Excel verification.

## Operations

See [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for the daily operating model, failure recovery, manual refresh procedures, and bootstrap guidance.
