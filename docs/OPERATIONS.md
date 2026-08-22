# Operations Runbook

## Normal production schedule

`MobiWork DMS Sync` uses two automatic schedules in `Asia/Ho_Chi_Minh`:

```text
HH:05 every hour -> SYNC_SCOPE=today
09:00 every day  -> SYNC_SCOPE=yesterday
```

Both use:

```text
SYNC_MODE=incremental
LOOKBACK_DAYS=1
DRY_RUN=false
```

The hourly run repeatedly refreshes the current Vietnam calendar day. The 09:00 run finalizes D-1. Example on 22/08: the 09:00 run refreshes 21/08; 09:05 and later hourly runs refresh 22/08.

Minute `05` is deliberate: it reduces top-of-hour GitHub queue pressure and prevents the hourly trigger from being identical to the 09:00 D-1 trigger. The shared concurrency lock ensures only one production job writes SharePoint at a time, so the 09:05 job waits if the 09:00 finalization is still running.

GitHub-hosted runners can start a few minutes after the configured trigger when queue pressure exists. The data freshness target is therefore approximately one hour plus runner/API processing time, not sub-minute real time.

## Incremental scopes

The runner supports:

```text
today      -> current Vietnam date
yesterday  -> previous Vietnam date
lookback   -> previous N days
```

Automatic hourly runs use `today`; automatic 09:00 runs use `yesterday`. `lookback` is for controlled manual recovery when older MobiWork data is corrected.

## Execution policy

All enabled reports are attempted independently in this order:

```text
visit
new_customer
order
bill
```

A failure in one report does not block the remaining reports. Each report writes its own result into `report_results` in the manifest.

Overall status rules:

- all reports succeed -> `success`;
- one or more reports fail -> `partial_failure`, and the workflow exits non-zero after all reports have been attempted;
- setup/configuration failure before report execution -> `failed`.

This policy maximizes data availability without hiding partial failures.

## Excel upload verification

SharePoint/Office may rewrite OOXML package metadata when an `.xlsx` file is uploaded. The resulting remote file can therefore have a different physical size or SHA-256 while retaining identical worksheet data.

For Excel files, production verifies the downloaded SharePoint workbook semantically using:

- worksheet names/order;
- every non-empty cell coordinate;
- cell data type;
- cell value.

A semantic mismatch fails closed. Physical package differences alone are accepted when worksheet business content is identical.

Existing dated files are replaced using a staged upload/promotion process with rollback protection. Hourly runs replace the same current-day filenames, so the document library does not accumulate 24 Excel copies per day.

## Manual refresh

Use `workflow_dispatch` for controlled recovery, immediate refresh, or investigation.

For the latest current-day data:

```text
sync_mode=incremental
sync_scope=today
lookback_days=1
dry_run=false
```

To finalize/retry the previous day:

```text
sync_mode=incremental
sync_scope=yesterday
lookback_days=1
dry_run=false
```

For older corrections:

```text
sync_mode=incremental
sync_scope=lookback
lookback_days=N
dry_run=false
```

For inspection without SharePoint writes, set `dry_run=true`. Dry-run Excel outputs are retained as short-lived GitHub Actions artifacts.

## Production preflight vs CI

Hourly production intentionally does not rerun the full unit-test suite every hour. It performs a lightweight production preflight:

1. compile Python sources;
2. parse `config/reports.json`;
3. confirm at least one report is enabled;
4. validate runtime secrets/OIDC configuration before production writes.

Full code quality checks run in `.github/workflows/ci.yml` on pull requests and pushes to `main`:

- Python compilation;
- Ruff;
- unit tests;
- coverage threshold.

This separation keeps hourly refresh latency low without weakening change-control quality gates.

## Production checklist after code changes

Before merging a production change:

1. CI must be green.
2. Confirm the workflow still targets `MobiWorkDMS` on `/sites/Planning`.
3. Confirm hourly schedule remains `5 * * * *` with `Asia/Ho_Chi_Minh`.
4. Confirm D-1 finalization remains `0 9 * * *` with `Asia/Ho_Chi_Minh`.
5. Confirm scheduled runs remain `incremental` and `dry_run=false`.
6. Confirm all enabled reports remain in `config/reports.json`.
7. Confirm incremental-scope, semantic-verification and continue-on-report-error tests pass.
8. For data-contract changes, run a manual dry run and inspect schema/row counts.
9. For SharePoint/authentication changes, run a manual production sync before relying on automation.

## Failure behavior

The process fails safely for data-integrity problems such as:

- MobiWork `status=false`;
- missing/unexpected response data structures;
- incomplete pagination where API totals are configured;
- blank/missing required fields;
- duplicate configured business keys;
- duplicate order header/detail keys;
- Excel row-limit violations;
- Graph upload failure after retries;
- uploaded Excel workbook differing semantically from the generated workbook;
- staged replacement/promotion failure that cannot be verified.

Transient timeouts, rate limits, and temporary `5xx` responses are retried automatically with backoff.

## Troubleshooting order

When a production run fails:

1. Open the GitHub Actions **Job Summary**.
2. Check `Sync scope` to know whether the run targeted today, yesterday, or lookback.
3. Inspect the report table and identify failed report(s).
4. Download `sync_manifest.json` from the run artifact if needed.
5. Inspect the failed step log.
6. Check MobiWork HTTP status/message and pagination behavior.
7. Check Graph HTTP status, retry behavior, OIDC and SharePoint permissions.
8. Check the latest `_sync_runs/YYYY/MM/*.json` audit manifest in SharePoint.

Do not keep increasing retry counts to hide deterministic failures. Separate transient failures (`429`, `5xx`, timeout) from authentication, schema, permission, and data-contract failures.

## Audit manifest

Each production run records:

- run ID and timestamps;
- sync scope;
- enabled reports;
- source rows per report;
- report-level success/failure;
- generated filename;
- local SHA-256/size;
- SharePoint target and URL;
- Excel verification mode;
- successful/failed report counts;
- overall status;
- error summary when applicable.

The local manifest is `output/sync_manifest.json`; production also uploads it to:

```text
_sync_runs/YYYY/MM/<run_id>.json
```

Hourly execution intentionally creates hourly audit JSON records. These are operational trace records, not duplicate business Excel files.

## Bootstrap history

Bootstrap is a manual, resumable historical mode. It scans newest-to-oldest by month and saves progress to:

```text
_sync_state/bootstrap.json
```

If a transient failure occurs, rerun bootstrap normally to resume from the checkpoint.

Use `reset_bootstrap_state=true` only when intentionally restarting history because of a mapping/schema change or a required historical rescan.

## Authentication

Microsoft authentication flow:

```text
GitHub Actions OIDC
    -> azure/login
    -> Azure CLI session
    -> AzureCliCredential
    -> Microsoft Graph
```

The Python client refreshes Graph tokens before expiry and forces refresh after a `401`. No Microsoft client secret is required.

Required secrets:

```text
MOBIWORK_USER
MOBIWORK_TOKEN
AZURE_CLIENT_ID
AZURE_TENANT_ID
```

## Rollback

Production changes should be merged through pull requests.

If a release causes deterministic failure:

1. Do not repeatedly rerun the same failing release.
2. Revert the responsible PR on `main`.
3. Confirm CI passes for the revert.
4. Run a dry run when data mapping changed.
5. Run a production incremental sync when SharePoint/authentication behavior changed.
6. Fix the issue on a new branch.

Deterministic dated filenames make corrected reruns safe: the affected day is replaced instead of duplicated.

## Change-control rules

Treat these as data-contract changes and review them carefully:

- SharePoint folder names;
- Excel worksheet/column names;
- business keys;
- order/detail table structure;
- timezone conversion;
- MobiWork date filters;
- incremental scope/date resolution;
- pagination semantics;
- semantic workbook verification rules;
- bootstrap checkpoint/stop behavior.
