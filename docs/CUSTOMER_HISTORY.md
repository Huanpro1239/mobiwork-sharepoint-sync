# Customer History Master

`KPI/History/customer_history.csv` is the compact source of truth used to classify a customer as **Mới** or **Cũ** without re-reading years of Visit/Order workbooks on every KPI run.

## Why it exists

For New/Old classification, the KPI engine only needs the earliest known customer activity. Reading every monthly workbook for many years is unnecessary and eventually becomes too slow and memory-heavy.

The history master therefore keeps exactly one row per normalized `ma_kh`.

## Schema

| Column | Meaning |
|---|---|
| `ma_kh` | Customer code |
| `ten_kh` | Latest non-empty customer name seen |
| `first_visit_date` | Earliest known visit date |
| `first_order_date` | Earliest known order date |
| `first_activity_date` | Minimum of first visit/order |
| `last_visit_date` | Latest known visit date |
| `last_order_date` | Latest known order date |
| `last_activity_date` | Maximum of last visit/order |
| `ever_visit` | Whether any visit has ever been observed |
| `ever_order` | Whether any order has ever been observed |
| `schema_version` | History schema version |
| `updated_at_utc` | Last time this customer row was updated |

The invariant is:

```text
first_activity_date = MIN(first_visit_date, first_order_date)
```

An incremental update may move `last_*` dates forward, but it must never move any `first_*` date forward.

## First run

If the SharePoint history file does not exist, the pipeline performs a one-time bootstrap:

```text
all historical Visit monthly masters
      +
all historical Order monthly masters
      ↓
process one workbook at a time
      ↓
keep only min/max customer dates
      ↓
KPI/History/customer_history.csv
```

This bootstrap is memory-bounded because historical workbooks are not concatenated into one giant DataFrame.

A dry run builds the history locally at `runtime/output/customer_history.csv` but does not publish it.

## Normal runs

Once the history master exists, normal KPI runs only download workbook contents for:

```text
M-1 + M
```

Those recent facts update the compact master while the earliest known dates are preserved.

```text
customer_history.csv
        +
Visit/Order M-1 + M
        ↓
updated customer_history.csv
        ↓
KPI New/Old classification
```

The monthly-folder tree may still be listed to discover canonical files, but years of workbook contents are no longer downloaded or loaded into memory.

## New / Old rule

For KPI month `M`:

```text
first_activity_date < first day of M  → Cũ
first_activity_date >= first day of M → Mới
missing first_activity_date           → Không rõ
```

This means a customer first seen in M-1 and visited again in M is correctly treated as **Cũ** in M.

## Safety behavior

- Duplicate normalized `ma_kh` rows in the history file are rejected.
- Blank customer codes are rejected.
- Missing history for a customer currently under KPI review produces a warning and `Không rõ`; it must not silently manufacture a New customer date.
- Promotional order rows (`is_km=True`) are excluded from history/order KPI processing.
- The remote history file is only uploaded when the full non-dry-run pipeline reaches the publish stage.

## Configuration

Default remote path:

```text
KPI/History/customer_history.csv
```

Override with:

```text
CUSTOMER_HISTORY_REMOTE_PATH=KPI/History/customer_history.csv
```

## Operational check

After the first successful production run, confirm SharePoint contains:

```text
MobiWorkDMS/
└─ KPI/
   └─ History/
      └─ customer_history.csv
```

On subsequent runs, `run_manifest.json` records:

- history row count;
- whether history was initialized in that run;
- number of source files used for bootstrap;
- number of recent M-1/M source files processed;
- the remote history path.
