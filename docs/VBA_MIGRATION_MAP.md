# VBA migration map — File tính kế hoạch BẢN CẢI TIẾN V2

## Workbook profile

- 25 worksheets, including 2 hidden helper sheets.
- 40 VBA modules/classes extracted from `vbaProject.bin`.
- The current `Call_All.ChayTuDong_PAD` orchestrates 9 VBA steps.
- The workbook also contains several thousand formulas. The heaviest sheets are `Mua hang`, `Phan tich ABC`, `Phan bo NVL ngay`, and `Ke hoach SX tuan`.

## Current 9-step VBA pipeline

| # | VBA procedure | Main source | Main destination | Python target |
|---|---|---|---|---|
| 1 | `TongHopDuLieu_SP_FC_ThangNay` | Setting B14/B16, sales reports | `FC thang nay` T:AE | `planning.vba_port.aggregate_sales_actual` |
| 2 | `GuiKho_ChayCaHai` | Setting B11/B12, `Chi tiet` J/AG | `FC thang nay` O/P | `aggregate_gui_kho` + code map |
| 3 | `Run_Nokho_Complete` | Setting B3/B1/B2 | `Nokho` D:F, formula G | `nokho_col_d/e` + stock map + `nokho_balance` |
| 4 | `Lay_PO` | Setting B17, `REPORT_DONMUAHANG` | `PO` | `extract_open_po` |
| 5 | `CapNhatCotD_KeHoachNhapNVL_TuB8` | Setting B8, source sheet #2 B/H | `Ke hoach nhap NVL` D | V2 material-stock adapter |
| 6 | `Run_TinhUngHang_AllInOne` | Setting B2/B4/B5/B6 | `Tinh ung hang` D:F | divided-stock functions |
| 7 | `Run_FCThangNay_ColM_SumAOAP` | Setting B9/B10 | `FC thang nay` M | `sum_two_divided_stocks` |
| 8 | `LayDuLieuXuatKho` | Setting B15/B18 | `Data xuat kho thang truoc` | `aggregate_xuat_kho` |
| 9 | `Run_Update_TonTT_To_KHSX_Tuan` | Setting B13 | `Ke hoach SX tuan` I | `map_ton_tt` |

## Non-pipeline VBA that still matters

- `Capnhat_BOM.UpdateFlatBOM`: recursive BOM explosion. Ported as `explode_bom` with explicit cycle failure.
- `Dashboard_NVL.RefreshDashboardNVL`: presentation only; should be generated from Python result tables, not treated as business logic.
- `Tong_Hop`, `modTaoINKIMOI`: print/layout/report formatting; keep as downstream presentation until calculation cutover.
- `modCommon`: SharePoint path handling, number/code normalization, workbook cache. Replace with Microsoft Graph + Python normalization.

## Formula engine dependencies

```text
Sales/Stock inputs
  -> FC thang nay / Nokho / Tinh ung hang
  -> BOM
  -> Ke hoach nhap NVL
  -> Mua hang
  -> Phan bo NVL ngay
  -> Ke hoach SX tuan / Ke hoach SX ngay / KHSX
  -> Phan tich ABC / Dashboard NVL / Tong hop
```

The most dangerous mistake would be to migrate only VBA while leaving `Mua hang`, `Phan bo NVL ngay`, and `Ke hoach SX tuan` as required Excel-calculation steps. GitHub Actions has no Excel Desktop calculation engine, so full unattended operation requires these formulas to be ported to Python or reduced to presentation formulas only.

## Important technical risks found

1. SharePoint access currently relies on `Workbooks.Open(https://...)` and the desktop user's signed-in Office session. This is not headless-safe.
2. `Call_All` treats many internally handled failures as successful because several procedures display an error and exit instead of re-raising it. The summary can therefore show `[OK]` for a failed business step.
3. Source URLs are hard-coded in worksheet cells and mix encoded/unencoded SharePoint paths. Renames and copied sharing links can break the macro.
4. Duplicate semantics are inconsistent by design and must be preserved in tests: some modules sum duplicates, some take the first occurrence, and `Nokho` D/E take the last occurrence.
5. Code conversion `1 <-> 2` is business logic, not a formatting detail. It appears in several source-specific paths and must be centralized.
6. Current planning logic is split between VBA and formula cells, making lineage and regression testing difficult.

## Target state

Excel/SharePoint becomes a UI + storage surface. GitHub Actions runs the deterministic calculation engine and publishes versioned/shadow outputs through Microsoft Graph. After parity is demonstrated, the engine may publish the canonical production tables while the original `.xlsm` remains a rollback artifact.
