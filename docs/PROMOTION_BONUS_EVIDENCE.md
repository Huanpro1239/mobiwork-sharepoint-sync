# Promotion Bonus: DMS request evidence still required

This PR is a diagnostic and isolated workflow change, not a verified replacement of Paybonus. It does not change visit/new_customer/order/bill, photo processing, monthly masters, quality gates, recovery or OIDC.

## Evidence as of 2026-10-06

The official OpenAPI document at https://dms.mobiwork.vn/openapi/openapi.json?v=1.13 documents GET /OpenAPI/V1/PromotionBonusReport with required id_ct (1–5 comma-separated IDs), optional projectID, assignTo, idcustomer and sttt. It documents neither report date parameters nor the sttt enum/default. The UI calculation label cannot safely be assigned a number. Catalogue date parameters fromdate/todate are documented for PromotionBonus only, with dd/MM/yyyy format. Their selection equivalence to the Paybonus “all” option remains unverified.

Previous authenticated report requests sent only id_ct for 98 programs and returned zero customer rows. This proves the old request does not reproduce the observed UI output; it does not prove which missing parameter, permission or backend behavior causes zero rows. Exact root cause remains unknown.

Found the DMS golden reference locally at C:/Users/Si Huan/Desktop/Mẫu.xlsx. It has 57 sheets, of which 56 contain customer tables; exactly 250 numbered customer rows. Its declared sheet dimensions are unreliable (A1:A1), so the reader resets them. Observed Khu vực distribution: Miền Trung 1B 215; Miền Trung 2 1; Miền Bắc > Hà Nội 16; Quận Hoàng Mai 1; Quận Thanh Xuân 1; Miền Bắc 1; blank 15. Preserve subdivisions; no program-ID-to-region inference. The file is not committed or uploaded.

The region output columns visible in the golden table are STT, Mã khách hàng, Tên khách hàng, SĐT, Địa chỉ, Nhà phân phối, Khu vực, Loại khách hàng, Nhóm khách hàng, Timepass, Chỉ tiêu (Kế hoạch/Thực hiện/Còn lại/Tỷ lệ), Kết quả. The report response currently has no customer rows to establish source field mappings or customer/program/level grain. Region-sheet exporter and exact monetary comparisons are deferred until that schema is observed. Existing generic workbook writer tests remain; no fabricated customer fixtures are presented as production verification.

## Safe diagnostic

Use existing MOBIWORK_USER and MOBIWORK_TOKEN environment variables (do not print them):

```powershell
$env:PYTHONPATH = "src"
python src/promotion_api_audit.py --from-date 2026-10-01 --to-date 2026-10-31 --program all --calculation-mode total_price_quantity --golden "C:\Users\Si Huan\Desktop\Mẫu.xlsx"
```

Omitted dates default to the current Vietnam calendar month. Specific --program must match a returned catalogue ID. --sttt accepts an explicit caller-supplied probe value using the documented parameter, but its meaning is always flagged unverified; no default magic value is sent. Dates filter only the catalogue; diagnostics explicitly report that no report date binding has been established. Only counts, field paths, parameter names and error types are saved in output/promotion_api_audit.json. No token, cookie, emails, customer values or API error messages are saved. Golden reference output contains counts/region distribution, not customer IDs. Duplicate customer rows across programs/levels are counted, not deduplicated.

GitHub Actions safe evidence run:

```text
report_scope=promotion_api_audit
bonus_from_date=2026-10-01
bonus_to_date=2026-10-31
bonus_program=all
bonus_sttt_probe=<leave blank until an observed value is available>
dry_run=true
```

No Azure/SharePoint or other reports run in audit mode. Authenticated DMS data is production data even with dry_run=true. Excel workbook artifact upload is removed; only audit manifests are retained. Production workbook destination is unchanged: 06_BaoCaoTraThuong/BaoCaoTraThuong_Current.xlsx.

## Isolated production scope

```text
report_scope=promotion_bonus_only
dry_run=false
bonus_from_date=2026-10-01
bonus_to_date=2026-10-31
bonus_program=all
```

Only Promotion Bonus and existing SharePoint auth/library resolution are eligible to run. This mode deliberately fails before any API fetch or SharePoint write while date/calculation/row mapping evidence is unresolved. It does not claim to export the requested October report and cannot replace the current workbook with a misleading empty snapshot. Existing all_reports behavior remains unchanged apart from workbook artifacts being disabled.

## What is needed to finish

An authenticated Paybonus page (the accessible browser currently redirects to sign-in), or a sanitized request captured for the October 1–31 / all / total price×quantity / no department/employee case. Needed: endpoint and method, parameter names and formats, selected calculation value, program selection representation, and a sanitized customer response schema. Do not provide cookies, Authorization headers, tokens, phone numbers or addresses. Once available, implement the request and region sheets, compare all 250 golden rows at customer+program+level grain, and test target/actual/remaining/rate values. Counts >0 are insufficient acceptance.
