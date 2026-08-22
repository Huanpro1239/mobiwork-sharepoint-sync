# MobiWork SharePoint Sync

Production-oriented ETL pipeline for exporting MobiWork DMS reports to Excel and publishing them to SharePoint automatically.

```text
GitHub Actions
    -> Python validation / retry layer
    -> MobiWork Open API
    -> normalized Excel workbooks
    -> Microsoft Graph
    -> SharePoint MobiWorkDMS
```

## Production target

- SharePoint host: `vikodacomvn.sharepoint.com`
- Site: `/sites/Planning`
- Document library: `MobiWorkDMS`
- Daily schedule: `09:00` in `Asia/Ho_Chi_Minh`
- Scheduled incremental lookback: 3 previous days

Refreshing several previous days intentionally replaces deterministic dated files, so late edits in MobiWork are captured without creating duplicate files.

## Reliability model

The pipeline is designed to fail safely instead of silently publishing incomplete data.

- MobiWork requests use throttling, timeout handling, exponential backoff with jitter, `Retry-After`, and retries for `429`/temporary `5xx` failures.
- Microsoft Graph requests refresh expired/rejected tokens and retry network, `429`, and temporary `5xx` failures.
- GitHub Actions uses a production concurrency lock so manual and scheduled syncs cannot write the same SharePoint targets simultaneously.
- Bootstrap history is checkpointed in SharePoint and resumes from the last completed month after an interruption.
- Configured business keys and required fields are validated before export.
- Reports that expose an API `total` can verify that pagination retrieved the complete dataset before any Excel file is written.
- Uploaded file size is checked against the local payload.
- Every run creates an audit manifest containing report row counts, SHA-256 file hashes, file sizes, target folders, and SharePoint URLs.

## Security model

- MobiWork credentials are stored only as GitHub Actions secrets.
- Microsoft authentication uses GitHub OIDC -> Microsoft Entra -> Azure CLI credential; no Microsoft client secret is stored in the repository.
- Access tokens are refreshed by the client instead of being cached for the entire long-running job.
- Production should use least privilege for Microsoft Graph, preferably `Sites.Selected` with access granted only to the Planning site.
- Never commit `.env`, tokens, passwords, Authorization headers, exported business data, or generated Excel files.

Required GitHub Actions secrets:

```text
MOBIWORK_USER
MOBIWORK_TOKEN
AZURE_CLIENT_ID
AZURE_TENANT_ID
```

`MOBIWORK_USER_ID` is accepted as a backward-compatible alternative to `MOBIWORK_USER` in the workflow.

## Enabled reports

Configuration lives in `config/reports.json`.

| Key | MobiWork API | SharePoint folder | Export |
|---|---|---|---|
| `visit` | `/OpenAPI/V1/VisitData` | `01_BaoCaoViengTham` | flat visit rows |
| `new_customer` | `/OpenAPI/V1/Customer` | `02_MoMoiKhachHang` | flat rows |
| `order` | `/OpenAPI/V1/Order` | `03_DonDatHang` | header + line items |
| `bill` | `/OpenAPI/V1/Bill` | `04_DonBanHang` | header + line items |

### Sales-order / bill model

MobiWork returns order lines in `san_pham`. Sold and promotional items stay in the same analytical table and are distinguished by `is_km`.

Each order workbook contains:

```text
DonHang
ChiTietSP
```

`DonHang` uses `ma_phieu` as the business key. `ChiTietSP` uses `ma_phieu + stt` as the line key.

The exporter also:

- preserves business codes such as `ma_sp = "00008"` as text;
- converts quantities and money fields from API strings to numeric Excel values;
- converts UTC API timestamps to `Asia/Ho_Chi_Minh` before writing Excel;
- adds `loai_hang` (`Bán hàng` / `Khuyến mãi`) while preserving the source `is_km` flag;
- rejects duplicate header or line keys instead of silently exporting ambiguous data.

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

`_sync_runs` is the permanent audit trail. `_sync_state/bootstrap.json` is the resumable history checkpoint.

## Run modes

### Incremental

Normal production mode. Scheduled runs refresh the previous 3 days.

```bash
python src/main.py --sync-mode incremental --lookback-days 3
```

### Bootstrap

Historical mode walks backward month by month from yesterday. After every fully completed month, the next cursor is saved to SharePoint. If the job fails, rerunning bootstrap continues from that checkpoint.

```bash
python src/main.py --sync-mode bootstrap
```

To intentionally discard a completed/current checkpoint and rescan from yesterday:

```bash
python src/main.py --sync-mode bootstrap --reset-bootstrap-state
```

### Dry run

Dry run fetches MobiWork and generates Excel without Microsoft authentication or SharePoint writes.

```bash
python src/main.py --sync-mode incremental --lookback-days 1 --dry-run
```

The GitHub workflow uploads dry-run workbooks as short-lived Actions artifacts.

## CI quality gates

Pull requests and pushes to `main` run:

1. Python byte-code compilation
2. Ruff static analysis
3. Unit tests
4. Branch-aware coverage reporting with a minimum threshold

Tests cover nested MobiWork expansion, pagination totals, required/business-key validation, sales-order normalization, Excel code preservation, timezone conversion, duplicate detail keys, and Microsoft Graph token refresh behavior.

## Production run audit

Each execution writes `output/sync_manifest.json`. Production runs also upload the same information to `_sync_runs` in SharePoint.

Example fields:

```json
{
  "run_id": "20260822T090000_123456_1",
  "mode": "incremental",
  "status": "success",
  "file_count": 12,
  "source_row_count": 45231,
  "files": [
    {
      "report": "bill",
      "source_rows": 2100,
      "filename": "DonBanHang_2026-08-21.xlsx",
      "sha256": "...",
      "remote_size_bytes": 123456
    }
  ]
}
```

The workflow publishes a concise version of this manifest to the GitHub Actions Job Summary after every run.

## Operating guidance

See [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for deployment checks, failure recovery, bootstrap procedures, and troubleshooting.
