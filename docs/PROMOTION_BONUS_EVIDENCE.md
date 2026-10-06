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

## Authenticated October diagnostic result

Run 37410012675 completed successfully with catalogue dates 01/10/2026–31/10/2026. It returned 57 catalogue programs and zero report customer rows; the report requests still contained only id_ct because date/calculation bindings are unknown. Golden reference is 250 rows, so the count comparison fails (0 != 250). Diagnostic success is not business verification success. No workbook/customer values were uploaded as artifacts. CI 37410154959 passed on 5e61fd9. The user will provide sanitized request parameters from their signed-in Chrome session; Chrome is not exposed to the connected computer-use tools.

## Direct API check using user-provided credentials

On 2026-10-06, direct BasicAuth calls to the documented OpenAPI host succeeded: dated October catalogue returned HTTP 200/status=true and 57 programs. All 57 id_ct-only report calls also returned HTTP 200/status=true, total=0/data=[]. No credentials or raw customer data were written into repository files or artifacts. Authentication acceptance does not establish report-level data permissions or exact cause of empty results.

Live nested catalogue fields include ctype.label/value (target-region criteria), gtype.label/value (customer-type criteria), ktype.label/value (channel criteria), ptype.label/value (promotion mechanic), settings.BoiSo/promotionType and products. These are program eligibility/configuration, not an observed customer-report region schema or a verified sttt enum. No fallback assigning customer regions from these fields is enabled. Diagnostic now captures sanitized nested catalogue field paths to support further investigation.

## Chrome evidence: POST report endpoint

User screenshots establish a different UI contract: POST https://dms.mobiwork.vn:3020/PromotionBonusReport. Query keys: orgid, projectID, projectName, assignTo, eeName, idcustomer, startDate, endDate. Empty employee/department/customer filters were observed. startDate=1790787600000 decodes to 2026-10-01 00:00:00 Asia/Ho_Chi_Minh, confirming epoch milliseconds for this captured start. endDate was literally NaN; valid end-date conversion, inclusivity and timezone semantics remain unverified. Body contains arrCT (array of program IDs); full body and calculation binding are not yet visible. Response top-level keys include result, arrChiTieu, arrTraThuong, arrFormElement and message; a result row includes _idCT and soSuatCT. Complete customer/region/target/actual row fields remain collapsed.

The UI request is demonstrably POST+arrCT+result, whereas the current OpenAPI request is GET+id_ct+data. This explains why the old implementation is not a replay of the UI contract, but does not establish the OpenAPI backend's exact reason for zero rows. These are separate hosts/endpoints; OpenAPI BasicAuth compatibility with the UI service is not proven. An anonymous, read-only POST probe of the observed request (one captured program, captured NaN retained only for diagnostic reproduction) returned HTTP 403 with code/message and no result. No OpenAPI credential/cookie was sent to this unverified UI-auth endpoint. Production exporter remains blocked.

Next evidence required: full Request payload from View source, a valid endDate request after calendar re-selection, and expanded result[0] schema with customer/contact values hidden. No region fallback, NaN date, guessed calculation value or guessed UI authentication is enabled.

## Corrected Chrome date query

A subsequent screenshot replaces endDate=NaN with 1793379600000. Observed startDate=1790787600000 and endDate=1793379600000 decode to 2026-10-01 00:00:00+07:00 and 2026-10-31 00:00:00+07:00 respectively. Thus both captured UI date values are epoch milliseconds at Vietnam local midnight. This does not establish whether the backend includes the entire last day; do not add a day or change the timestamp without evidence.

The new response preview shows result as an array of per-request/program envelopes containing result:[] and message:""; top-level arrChiTieu and arrTraThuong are empty and arrFormElement is present. No customer schema or 250-row match is established by this screenshot. Full POST body, selected calculation binding and the visible UI customer count for this exact corrected request remain needed. Do not treat the nested envelope array as customer rows or deduplicate program results.

## Complete captured request body

The user supplied the full POST JSON body: arrCT contains 57 program IDs and no other property. No sttt/calculation parameter is present in this captured body or query. Do not infer that a hidden mode equals an OpenAPI sttt value. The UI request builder now reconstructs the observed POST endpoint/query/arrCT format with runtime IDs and local-midnight epoch timestamps. Tests reproduce both captured October date values and 57-ID format; there is no five-ID OpenAPI batching limit applied to this different UI service. No customer/organization IDs are hard-coded into the builder, no authentication is fabricated and no request is executed by it. Export remains blocked pending authenticated nonempty response schema and UI calculation behavior.
