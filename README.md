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
- Do not commit `.env`, API tokens, client secrets, passwords, Authorization headers, or exported business data.
- Production should use the least-privilege SharePoint permission model (for example `Sites.Selected` plus a grant to the Planning site).

## Current API mapping status

### Visit report — verified

- GET `https://openapi.mobiwork.vn/OpenAPI/V1/VisitData`
- required date parameters: `tu_ngay`, `den_ngay`
- date format: `dd/mm/yyyy`
- optional filters `phong_ban_nv` and `ma_nv` are omitted to retrieve all available staff
- response rows live at `data`
- `data` is grouped by employee; each employee has nested `thoi_gian_vt`
- the pipeline explodes `thoi_gian_vt` so one Excel row represents one visit and inherits `ma_nv` + `ten_nhan_vien`

The new-customer and order reports remain disabled until their Swagger request/response shapes are verified.

## Required GitHub Actions secrets

MobiWork:

- `MOBIWORK_USER`
- `MOBIWORK_TOKEN`

Microsoft (required only when SharePoint upload is enabled):

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`

Do not store API endpoints as secrets unless they are genuinely confidential. Verified public endpoint paths are kept in `config/reports.json`.

## MobiWork mapping workflow

For each remaining report, use the MobiWork OpenAPI/Swagger page to verify:

1. HTTP method
2. request URL
3. from/to date parameter names
4. pagination parameters, if any
5. JSON path containing the records
6. whether records contain nested lists that must be exploded

Then update `config/reports.json` and enable only the verified report.

## Manual test

From GitHub Actions, run **MobiWork DMS Sync** with:

- `lookback_days = 1`
- `dry_run = true`

Dry-run mode calls MobiWork and generates Excel, but does not authenticate to or upload to SharePoint. The output is attached to the workflow as a short-lived artifact.

## Production schedule

The workflow is configured for `06:07` daily in `Asia/Ho_Chi_Minh`. Scheduled workflows only run from the default branch. Scheduled execution is additionally gated by the repository variable `PRODUCTION_ENABLED=true`, so merging the scaffold cannot accidentally run production before configuration is complete.

## Output layout

```text
MobiWorkDMS/
├── 01_BaoCaoViengTham/YYYY/MM/*.xlsx
├── 02_MoMoiKhachHang/YYYY/MM/*.xlsx
└── 03_DonHang/YYYY/MM/*.xlsx
```

The default lookback is three days so late MobiWork syncs/edits are refreshed automatically by replacing the same dated file.
