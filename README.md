# MobiWork SharePoint Sync

Python ETL pipeline for exporting **MobiWork DMS Open API** reports to analytics-friendly **Excel** monthly masters and publishing them to **Microsoft SharePoint** through **Microsoft Graph**. It is designed for unattended operation in **GitHub Actions**, with retries, data-quality checks, audit manifests, staged SharePoint replacement, and semantic workbook verification.

> Keywords: MobiWork DMS, SharePoint, Microsoft Graph, Excel ETL, GitHub Actions, Python, data pipeline.

## Why this project exists

A direct “API -> Excel -> overwrite SharePoint” script is easy to start but difficult to operate safely. This repository adds the controls needed for a long-running reporting pipeline:

- bounded API throttling, retries, jitter, and `Retry-After` handling;
- pagination and configured API-total integrity checks;
- required-field and business-key validation;
- preservation of text business codes such as `00008`;
- deterministic normalization of order headers and line items;
- one canonical workbook per report/month instead of daily-file sprawl;
- staged replacement and rollback for existing SharePoint files;
- semantic `.xlsx` verification after SharePoint/Office repackaging;
- independent report execution with an overall failed status on partial failure;
- JSON audit manifests for every production run;
- CI with compilation, Ruff, unit tests, and branch coverage.

## Architecture

```text
GitHub Actions / local runner
        |
        v
MobiWork Open API
        |
        v
validation + normalization
        |
        v
monthly Excel master
        |
        v
Microsoft Graph
        |
        v
SharePoint document library
        |
        +--> semantic workbook verification
        +--> _sync_runs audit JSON
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the module map, trust boundaries, storage contract, and failure model.

## Requirements

- Python 3.12
- MobiWork Open API credentials
- a Microsoft 365 / SharePoint document library
- Microsoft Graph access for the target SharePoint site
- Azure CLI authentication locally, or GitHub OIDC + Microsoft Entra in Actions

## Quick start

Clone the repository and install dependencies:

```bash
git clone https://github.com/Huanpro1239/mobiwork-sharepoint-sync.git
cd mobiwork-sharepoint-sync
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create your environment from the template:

```bash
cp .env.example .env
```

Do **not** commit `.env`. Export the variables into your shell or use your preferred local secret loader.

For a reusable report configuration, start from [`config/reports.example.json`](config/reports.example.json). The checked-in [`config/reports.json`](config/reports.json) is the active deployment profile for this repository.

Run a local no-write export:

```bash
export MOBIWORK_USER="..."
export MOBIWORK_TOKEN="..."
export SYNC_SCOPE=today
export LOOKBACK_DAYS=1
export DRY_RUN=true
python src/run_all_reports.py
```

Generated workbooks and manifests are written under `output/`, which is intentionally ignored by Git.

## Configuration

### MobiWork

Required runtime values:

| Variable | Purpose |
|---|---|
| `MOBIWORK_USER` | MobiWork API user identifier |
| `MOBIWORK_TOKEN` | MobiWork API token/password |
| `MOBIWORK_MIN_INTERVAL_SECONDS` | Minimum spacing between MobiWork requests |
| `MOBIWORK_MAX_RETRIES` | Retry limit for transient MobiWork failures |
| `MOBIWORK_TIMEOUT_SECONDS` | HTTP timeout |

Report mappings live in `config/reports.json`. Each report can define its endpoint, date parameters, pagination fields, response path, optional nested-list expansion, export mode, required fields, and primary key.

### SharePoint / Microsoft Graph

| Variable | Purpose |
|---|---|
| `SHAREPOINT_HOST` | SharePoint hostname |
| `SHAREPOINT_SITE_PATH` | Site path, for example `/sites/YourSite` |
| `SHAREPOINT_LIBRARY` | Target document-library name |
| `SHAREPOINT_DRIVE_ID` | Optional resolved Graph drive ID |
| `SHAREPOINT_MAX_RETRIES` | Retry limit for transient Graph failures |
| `SHAREPOINT_TIMEOUT_SECONDS` | HTTP timeout |

The production workflow in this repository contains deployment-specific SharePoint target values. **Before publishing a fork or using this workflow in another tenant, replace those values and review the Microsoft Entra permissions.**

## Enabled report model

The current deployment uses four report types:

| Key | MobiWork API | Excel shape |
|---|---|---|
| `visit` | `/OpenAPI/V1/VisitData` | flat `Data` sheet |
| `new_customer` | `/OpenAPI/V1/Customer` | flat `Data` sheet |
| `order` | `/OpenAPI/V1/Order` | `DonHang` + `ChiTietSP` |
| `bill` | `/OpenAPI/V1/Bill` | `DonHang` + `ChiTietSP` |

Order-style business keys are:

```text
DonHang   -> ma_phieu
ChiTietSP -> ma_phieu + stt
```

Historical MobiWork line items may omit `stt`. Valid source values are preserved. Only missing/invalid line numbers receive the first unused positive integer in source order. Duplicate valid source keys still fail closed.

## Storage model: one workbook per report/month

Each report/month has one canonical workbook:

```text
<SharePoint library>/
├── <report-folder>/YYYY/MM/<Report>_YYYY-MM.xlsx
└── _sync_runs/YYYY/MM/<run_id>.json
```

A hidden `_sync_date` column identifies each daily partition. A current-day run replaces only today’s partition; a previous-day run replaces only D-1. If a monthly master is missing, the pipeline rebuilds that report from day 01 of the month through the target date.

After a successful upload and verification, conservative cleanup can remove only recognized legacy files for that report/month:

- `<Report>_YYYY-MM-DD.xlsx`
- `<Report>_History_*.xlsx`
- orphan `__sync_tmp_*`, `__sync_backup_*`, and `__sync_failed_*` files

Unrelated files are not matched by this cleanup rule.

## Excel integrity

SharePoint/Office may rewrite OOXML package metadata, so an uploaded `.xlsx` can legitimately have different physical bytes or ZIP metadata while preserving worksheet data.

`SemanticSharePointClient` therefore downloads the uploaded workbook and compares:

- worksheet order and names;
- every non-empty cell coordinate;
- cell data type;
- cell value.

A business-cell mismatch fails closed. JSON audit files continue to use normal content/size integrity behavior.

## Sync scopes

`src/run_all_reports.py` is the production entry point.

```text
today      current Vietnam calendar day
yesterday  previous Vietnam calendar day
lookback   previous N days, maximum 31
```

Manual dry runs use the same normalization and monthly-master code path but skip SharePoint writes.

## GitHub Actions

The production workflow is `.github/workflows/mobiwork-sync.yml`.

Current deployment schedule in `Asia/Ho_Chi_Minh`:

| Schedule | Scope | Purpose |
|---|---|---|
| `HH:05` every hour | `today` | Refresh current-day data |
| `09:00` every day | `yesterday` | Finalize D-1 |

Both schedules share one concurrency group so only one production writer changes SharePoint at a time.

Required GitHub Actions secrets for the current OIDC deployment:

```text
MOBIWORK_USER
MOBIWORK_TOKEN
AZURE_CLIENT_ID
AZURE_TENANT_ID
```

`MOBIWORK_USER_ID` is accepted as a backward-compatible alternative to `MOBIWORK_USER`.

## Development and testing

Install development dependencies:

```bash
pip install -r requirements-dev.txt
```

Run the quality gate locally:

```bash
python -m compileall -q src tests
ruff check .
coverage run -m unittest discover -s tests -v
coverage report
```

CI runs the same categories of checks on pull requests and pushes to `main`.

## Operational behavior

Reports run independently. One report failure does not block attempts for the others, but the overall run exits non-zero when any report fails. The manifest distinguishes current-run source rows from rows stored in the monthly master.

For production recovery, failure modes, audit interpretation, and change-control checks, see [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Project layout

```text
.github/                 CI, production workflow, review template
config/                  active and reusable report mappings
docs/                    architecture and operations runbooks
src/                     API, Excel, monthly-master and SharePoint logic
tests/                   regression/unit tests
.env.example             local configuration template
requirements*.txt        pinned runtime/development dependencies
```

## Security

Never commit `.env`, MobiWork credentials, Microsoft tokens, Authorization headers, customer exports, or generated business Excel files. Use least-privilege Microsoft Graph / SharePoint permissions for the target site/library.

See [`SECURITY.md`](SECURITY.md) before publishing a deployment fork.

## Contributing

Pull requests are welcome. Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) and include regression coverage for changes to data contracts, business keys, pagination, authentication, Excel generation, or SharePoint replacement semantics.

## License

MIT — see [`LICENSE`](LICENSE).
