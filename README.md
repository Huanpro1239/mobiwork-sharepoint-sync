# MobiWork SharePoint Sync

Automated pipeline:

`GitHub Actions -> Python -> MobiWork Open API -> Excel -> Microsoft Graph -> SharePoint`

Target SharePoint:

- Host: `vikodacomvn.sharepoint.com`
- Site: `/sites/Planning`
- Document library: `MobiWorkDMS`

## Security model

- MobiWork credentials are GitHub Actions secrets.
- Microsoft authentication uses GitHub OIDC / Microsoft Entra federated credentials.
- Do not commit `.env`, API tokens, client secrets, passwords, or exported business data.
- Production should use the least-privilege SharePoint permission model (for example `Sites.Selected` plus a grant to the Planning site).

## Status

The repository scaffold is intentionally safe-by-default: all reports in `config/reports.json` start with `"enabled": false` until the real MobiWork Swagger endpoints, parameters, pagination and response paths are verified.

## Required GitHub Actions secrets

- `MOBIWORK_USER`
- `MOBIWORK_TOKEN`
- `MOBIWORK_VISIT_URL`
- `MOBIWORK_CUSTOMER_URL`
- `MOBIWORK_ORDER_URL`
- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`

Never paste secret values into source files.

## MobiWork mapping

For each report, use the MobiWork OpenAPI/Swagger page to verify:

1. HTTP method
2. request URL
3. from/to date parameter names
4. pagination parameter names and page size
5. JSON path containing the rows (for example `data` or `data.items`)

Then update `config/reports.json` and set the verified report to `"enabled": true`.

## Manual test

From GitHub Actions, run **MobiWork DMS Sync** with:

- `lookback_days = 1`
- `dry_run = true`

Dry-run mode calls MobiWork and generates Excel, but does not authenticate to or upload to SharePoint. The output is attached to the workflow as a short-lived artifact.

## Production schedule

The workflow is configured for `06:07` daily in `Asia/Ho_Chi_Minh`. Scheduled workflows only run from the default branch. Scheduled execution is additionally gated by the repository variable `PRODUCTION_ENABLED=true`, so merging the scaffold cannot accidentally call production APIs before configuration is complete.

## Output layout

```text
MobiWorkDMS/
├── 01_BaoCaoViengTham/YYYY/MM/*.xlsx
├── 02_MoMoiKhachHang/YYYY/MM/*.xlsx
└── 03_DonHang/YYYY/MM/*.xlsx
```

The default lookback is three days so late MobiWork syncs/edits are refreshed automatically by replacing the same dated file.
