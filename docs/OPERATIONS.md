# Operations Runbook

## Normal production schedule

The `MobiWork DMS Sync` workflow runs every day at **09:00 Asia/Ho_Chi_Minh**.

Normal scheduled execution uses:

```text
SYNC_MODE=incremental
LOOKBACK_DAYS=3
DRY_RUN=false
```

The three-day refresh window is deliberate: the same deterministic daily filenames are replaced, allowing late MobiWork edits to be reflected without accumulating duplicate files.

## First deployment checklist

Before merging a production change:

1. Confirm CI is green.
2. Run `MobiWork DMS Sync` manually with `incremental`, `lookback_days=1`, `dry_run=true`.
3. Download the dry-run artifact and inspect the Excel schema and row counts.
4. Confirm GitHub secrets exist: `MOBIWORK_USER`, `MOBIWORK_TOKEN`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`.
5. Confirm the Entra federated credential trusts this repository/workflow context.
6. Confirm the application/service principal can access only the required Planning SharePoint site.
7. Run a manual non-dry incremental sync for one day.
8. Confirm the Job Summary and SharePoint `_sync_runs` audit manifest.
9. Allow the next scheduled run to proceed automatically.

## Failure behavior

The process is fail-closed for data-integrity problems. It does not intentionally upload a report after any of these checks fail:

- MobiWork response reports `status=false`;
- configured response data is missing or has an unexpected type;
- API `total` does not match fetched pagination count;
- required fields are blank/missing;
- configured primary/business keys are duplicated;
- order header/detail keys are duplicated;
- generated worksheet exceeds Excel row limits;
- Microsoft Graph upload fails after retries;
- SharePoint-reported uploaded size differs from the local payload.

Transient network/rate-limit failures are retried automatically with backoff.

## Troubleshooting order

When a production run fails, inspect in this order:

1. GitHub Actions **Job Summary**.
2. `output/sync_manifest.json` from the run artifact, if available.
3. The failed step logs.
4. MobiWork HTTP status/message.
5. Microsoft Graph HTTP status and `Retry-After` behavior.
6. Entra OIDC configuration and SharePoint permissions.
7. The latest `_sync_runs/YYYY/MM/*.json` manifest in SharePoint.

Do not solve API failures by increasing retries indefinitely. Determine whether the failure is transient (`429`, `5xx`, timeout) or deterministic (authentication, schema, permission, invalid data).

## Bootstrap history

Bootstrap scans history newest-to-oldest by month.

```text
Yesterday
  -> current month
  -> previous month
  -> ...
```

After a month is fully processed and uploaded, the next cursor is written to:

```text
_sync_state/bootstrap.json
```

If the workflow fails later, rerun bootstrap normally. It resumes from the saved cursor.

### Reset bootstrap

Use `reset_bootstrap_state=true` only when intentionally rescanning history from yesterday, for example after changing a historical endpoint/schema or correcting an earlier mapping.

Do not reset a long-running bootstrap simply because a transient failure occurred; normal rerun is designed to resume.

## Data model: DonBanHang

The `/OpenAPI/V1/Bill` response is treated as:

```text
Bill header: ma_phieu
    |
    +-- san_pham[]
          line key: ma_phieu + stt
          is_km=false -> Bán hàng
          is_km=true  -> Khuyến mãi
```

The workbook contains `DonHang` and `ChiTietSP`. Promotional products are not split into a separate table because `is_km` is an attribute of the line item.

If MobiWork changes this schema, update the fixture/tests before changing production mapping.

## Audit manifest

A successful production manifest records:

- run ID and UTC timestamps;
- sync mode;
- enabled report keys;
- source row count per exported report file;
- local SHA-256;
- local/remote file sizes;
- SharePoint target folder and `webUrl`;
- overall status.

A failed run also records the exception class/message whenever the process has progressed far enough to write the manifest.

## Authentication

Microsoft authentication flow:

```text
GitHub Actions OIDC
    -> azure/login
    -> Azure CLI session
    -> AzureCliCredential
    -> Microsoft Graph access token
```

The Python client refreshes the Graph access token before expiry and forces refresh after a `401` response.

No Microsoft client secret is required by this design.

## Rollback

All production changes should be merged through pull requests.

If a new release causes deterministic production failure:

1. Stop manually rerunning the same failing workflow.
2. Revert the responsible PR on `main`.
3. Confirm CI for the revert.
4. Run one-day dry-run.
5. Run one-day production incremental sync.
6. Investigate the failed change in a new branch.

Because daily filenames are deterministic, a corrected incremental rerun safely replaces affected files.

## Change-control rules

Treat these as breaking data-contract changes and review them carefully:

- renaming SharePoint folders;
- renaming Excel worksheets or columns;
- changing business keys;
- splitting/merging detail tables;
- changing timezone conversion;
- changing MobiWork date filters (`kieu_ngay`, from/to parameters);
- changing pagination semantics;
- changing bootstrap stop/reset behavior.
