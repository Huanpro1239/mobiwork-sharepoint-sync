# Vikoda Planning Engine — implementation model

## Target architecture

```text
SharePoint source workbooks
        |
        v
Microsoft Graph download (existing SharePointClient)
        |
        v
Raw adapters (sheet/range contracts)
        |
        v
Normalization layer
  - product code 1 <-> 2 mapping
  - pack divisor / unit conversion
  - Vietnamese number/text normalization
        |
        v
Business engines
  1. sales actual / FC comparison
  2. finished-goods stock + warehouse debt
  3. open PO + material stock
  4. BOM explosion + material requirement
  5. material shortage + purchase suggestion
  6. production capacity / lot / calendar scheduler  [cutover phase]
        |
        v
Validation / parity tests / run manifest
        |
        +--> shadow xlsx/json on SharePoint
        +--> later: canonical planning tables
```

## Why shadow mode first

The current workbook combines 40 VBA modules with complex dynamic-array formulas. A single-step rewrite has high operational risk. Shadow mode reads the same sources, calculates Python outputs, but writes to `_PlanningEngine/shadow` without changing the production workbook. Every migrated rule should pass parity checks against the existing workbook before cutover.

## Recommended migration sequence

### Phase 1 — source/ETL parity

Port the 9 `Call_All` steps and BOM explosion. This removes all `Workbooks.Open` and manual update buttons from the critical data acquisition path.

Exit criteria:

- same product/material row counts;
- same duplicate-handling semantics;
- stock/PO/sales figures match workbook values within configured tolerance;
- no source file is opened by Excel Desktop.

### Phase 2 — material planning parity

Port stable formulas from:

- `Tinh ung hang` G:L;
- `BOM` F:G;
- `Ke hoach nhap NVL`;
- `Mua hang` net-requirement / MOQ / lead-time rules;
- `Phan bo NVL ngay` shortage-date logic;
- `Phan tich ABC` classification.

Exit criteria: 10 consecutive production runs with no unexplained material/purchase variance.

### Phase 3 — production scheduler

Port `Ke hoach SX tuan`, `Helper_SX_2Line`, `Ke hoach SX ngay`, and `KHSX` into explicit scheduling rules:

- line eligibility;
- product groups and special lines;
- capacity per shift/day;
- lotsize/minimum batch;
- current production / already-produced quantity;
- Sunday and holiday rules;
- two-line conflicts;
- earliest required dates;
- warehouse/finished-goods constraints.

This layer should be implemented as an optimizer/scheduler, not as a direct translation of nested `LET` formulas.

### Phase 4 — cutover

1. Freeze the `.xlsm` as rollback-only.
2. Publish canonical calculation tables from Python.
3. Keep Excel Online files for reporting/printing only.
4. Add failure notification and audit retention.

## Automation

`planning-engine.yml` runs at four Vietnam-local checkpoints Monday-Saturday and also supports manual dry runs. GitHub OIDC authenticates to Microsoft Entra; no Microsoft client secret is required in the workflow.

## Security

Use the same least-privilege Entra application already used by the existing SharePoint sync. Ideally grant site-scoped permission only to `/sites/Planning`. Never commit access tokens, `.env` files, or source business workbooks to GitHub.
