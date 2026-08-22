# Operations Runbook

## Normal production schedule

`MobiWork DMS Sync` uses two automatic schedules in `Asia/Ho_Chi_Minh`:

```text
HH:05 every hour -> SYNC_SCOPE=today
09:00 every day  -> SYNC_SCOPE=yesterday
```

Scheduled runs use `LOOKBACK_DAYS=1` and `DRY_RUN=false`. Minute `05` reduces top-of-hour queue pressure. The shared concurrency group allows only one production writer at a time, so the 09:05 current-day run waits if the 09:00 D-1 finalization is still active.

## Monthly master behavior

Each report/month has exactly one canonical business workbook:

```text
BaoCaoViengTham_YYYY-MM.xlsx
MoMoiKhachHang_YYYY-MM.xlsx
DonDatHang_YYYY-MM.xlsx
DonBanHang_YYYY-MM.xlsx
```

A hidden `_sync_date` column identifies the daily partition. Normal hourly execution downloads the existing monthly master, replaces only the target partition, uploads the same canonical filename, verifies the workbook, and leaves all other dates unchanged.

If the canonical master is missing, that report is rebuilt from day 01 of the month through the target date. This first rebuild can take substantially longer than a normal hourly update, especially for paginated Customer data.

After successful upload and semantic verification, cleanup removes only legacy files that match the same report/month:

- `Report_YYYY-MM-DD.xlsx`;
- `Report_History_*.xlsx`;
- orphan `__sync_tmp_*`, `__sync_backup_*`, and `__sync_failed_*` staging files.

Cleanup never runs before the canonical master has been verified. The runtime no longer contains a bootstrap/history-file mode, so cleanup cannot be undone later by an old code path.

## Incremental scopes

```text
today      -> current Vietnam date
yesterday  -> previous Vietnam date
lookback   -> previous N days, maximum 31
```

Automatic hourly runs use `today`; automatic 09:00 runs use `yesterday`. Use `lookback` only for controlled correction/backfill. All scopes use the same monthly-master implementation.

## Execution policy

Enabled reports run independently in this order:

```text
visit
new_customer
order
bill
```

One report failure does not block later reports. Overall status is:

- all report executions succeed -> `success`;
- one or more fail after others succeed -> `partial_failure` and non-zero workflow exit;
- setup/configuration failure before report execution -> `failed`.

## Order/Bill line-number rule

`ChiTietSP` uses `ma_phieu + stt` as its business key. Historical MobiWork data can contain line items with a missing or invalid `stt`.

Production handling is conservative:

1. preserve every valid positive integer `stt` supplied by MobiWork;
2. for only missing/invalid values, assign the first unused positive integer in source order;
3. run the normal duplicate-key check after normalization;
4. never silently repair duplicate valid source `stt` values.

This prevents data loss while still surfacing genuine duplicate-key defects.

## Excel upload verification

SharePoint/Office may rewrite OOXML package metadata, so `.xlsx` byte size and SHA-256 can change after upload even when worksheet data is unchanged.

For Excel, production verifies the downloaded workbook semantically using:

- worksheet names/order;
- every non-empty cell coordinate;
- cell data type;
- cell value.

Semantic mismatch fails closed. JSON/audit files continue to use ordinary byte/size integrity checks. Existing canonical Excel files are replaced through staged upload/promotion with rollback protection.

## Manual refresh

Latest current-day data:

```text
sync_scope=today
lookback_days=1
dry_run=false
```

Retry/finalize previous day:

```text
sync_scope=yesterday
lookback_days=1
dry_run=false
```

Older correction, up to 31 days:

```text
sync_scope=lookback
lookback_days=N
dry_run=false
```

For inspection without SharePoint writes, set `dry_run=true`. Do not reintroduce `History_*.xlsx` exports for older recovery; add a reviewed monthly-master backfill capability instead if a longer horizon becomes necessary.

## Production preflight vs CI

Hourly production performs only the checks needed before runtime writes:

1. compile Python sources;
2. parse `config/reports.json`;
3. confirm at least one report is enabled;
4. validate MobiWork credentials;
5. validate Microsoft OIDC configuration when SharePoint writes are enabled.

Full change-control CI runs on pull requests and pushes to `main`:

- Python compilation;
- Ruff;
- unit tests;
- coverage threshold.

## Audit manifest

Every run writes `output/sync_manifest.json` and production uploads it to:

```text
_sync_runs/YYYY/MM/<run_id>.json
```

Important counters:

- `source_rows`: rows fetched for that target date in the current execution;
- `master_rows`: total rows in that report's monthly master after the update;
- `source_row_count`: sum of current-run source rows for successful report executions;
- `master_row_count`: sum of monthly-master rows written by successful report executions.

The audit folder intentionally contains one JSON record per execution. These JSON records are operational evidence, not duplicate business Excel files.

## Failure behavior

Production fails safely for deterministic integrity problems including:

- MobiWork `status=false`;
- missing/unexpected response structures;
- incomplete pagination where API totals are configured;
- missing configured required fields;
- duplicate business keys;
- invalid Excel size;
- Graph upload failure after retries;
- semantic workbook mismatch;
- staged replacement/promotion failure that cannot be verified.

Transient timeouts, rate limits, and temporary `5xx` responses are retried automatically with backoff. Do not increase retry counts to hide deterministic schema/data errors.

## Troubleshooting order

When a production run fails:

1. Open the GitHub Actions Job Summary.
2. Confirm `sync_scope` and target date.
3. Identify failed report(s) in `report_results`.
4. Inspect `sync_manifest.json` from the artifact or SharePoint `_sync_runs`.
5. Inspect the failed step log.
6. Separate MobiWork data-contract errors from transient API/network errors.
7. Check Graph/OIDC/SharePoint only when the failure occurs after export.

A report that failed before upload keeps its existing SharePoint master unchanged. Other reports can still complete successfully.

## Authentication

```text
GitHub Actions OIDC
    -> azure/login
    -> Azure CLI session
    -> AzureCliCredential
    -> Microsoft Graph
```

Required secrets:

```text
MOBIWORK_USER
MOBIWORK_TOKEN
AZURE_CLIENT_ID
AZURE_TENANT_ID
```

## Change-control checklist

Before merging a production change:

1. CI must be green.
2. Confirm hourly schedule is `5 * * * *` with `Asia/Ho_Chi_Minh`.
3. Confirm D-1 finalization is `0 9 * * *` with `Asia/Ho_Chi_Minh`.
4. Confirm scheduled runs remain non-dry-run.
5. Confirm all four expected reports remain enabled.
6. Confirm monthly-master partition replacement tests pass.
7. Confirm semantic verification tests pass.
8. Confirm report-isolation tests pass.
9. Confirm no workflow or runtime path can generate legacy daily/history business workbooks.
10. For data-contract changes, run production validation before considering the issue closed.
