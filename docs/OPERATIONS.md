# Operations Runbook

## Normal production schedule

`MobiWork DMS Sync` runs every day at **09:00 Asia/Ho_Chi_Minh**.

Scheduled execution is fixed to:

```text
SYNC_MODE=incremental
LOOKBACK_DAYS=1
DRY_RUN=false
```

The scheduled run refreshes **D-1 only**. Example: the 22/08 run refreshes 21/08.

GitHub schedules are configured for 09:00 local time, but the hosted runner may actually start a few minutes later when GitHub has queue pressure. The intended trigger remains 09:00.

## Daily execution policy

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

Existing dated files are replaced using a staged upload/promotion process with rollback protection.

## Manual refresh

Use `workflow_dispatch` for controlled recovery or investigation.

Normal manual refresh settings:

```text
sync_mode=incremental
lookback_days=1
dry_run=false
reset_bootstrap_state=false
```

If a known MobiWork correction affects older data, increase `lookback_days` only enough to cover the affected period. Do not routinely refresh several old days without a business reason.

For inspection without SharePoint writes:

```text
dry_run=true
```

Dry-run Excel outputs are retained as short-lived GitHub Actions artifacts.

## Production checklist after code changes

Before merging a production change:

1. CI must be green.
2. Confirm the workflow still targets `MobiWorkDMS` on `/sites/Planning`.
3. Confirm scheduled mode remains `incremental`, `lookback_days=1`, `dry_run=false`.
4. Confirm all enabled reports remain in `config/reports.json`.
5. Confirm semantic Excel verification tests pass.
6. Confirm continue-on-report-error tests pass.
7. For data-contract changes, run a manual one-day dry run and inspect schema/row counts.
8. For SharePoint/authentication changes, run a manual one-day production sync before relying on the next schedule.

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
2. Inspect the report table and identify failed report(s).
3. Download `sync_manifest.json` from the run artifact if needed.
4. Inspect the failed step log.
5. Check MobiWork HTTP status/message and pagination behavior.
6. Check Graph HTTP status, retry behavior, OIDC and SharePoint permissions.
7. Check the latest `_sync_runs/YYYY/MM/*.json` audit manifest in SharePoint.

Do not keep increasing retry counts to hide deterministic failures. Separate transient failures (`429`, `5xx`, timeout) from authentication, schema, permission, and data-contract failures.

## Audit manifest

Each production run records:

- run ID and timestamps;
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
4. Run a one-day dry run when data mapping changed.
5. Run a one-day production incremental sync when SharePoint/authentication behavior changed.
6. Fix the issue on a new branch.

Daily filenames are deterministic, so a corrected rerun safely replaces the affected day.

## Change-control rules

Treat these as data-contract changes and review them carefully:

- SharePoint folder names;
- Excel worksheet/column names;
- business keys;
- order/detail table structure;
- timezone conversion;
- MobiWork date filters;
- pagination semantics;
- semantic workbook verification rules;
- bootstrap checkpoint/stop behavior.
