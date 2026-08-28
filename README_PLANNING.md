# Planning Engine V1

This branch extends the existing SharePoint sync repository with a SharePoint-native planning calculation engine migrated from `File tính kế hoạch - BẢN CẢI TIẾN_V2.xlsm`.

V1 is intentionally **shadow mode**. It ports the VBA ETL/stock-reconciliation rules and writes `planning_shadow.xlsx` plus `planning_manifest.json` without replacing the production workbook.

Before cutover, verify the Graph document-library name and `planning_master_path`, run `workflow_dispatch` with `dry_run=true`, and compare shadow results with the current workbook. The next migration target is formula parity for `Mua hang`, `Phan bo NVL ngay`, and `Ke hoach SX tuan`.
