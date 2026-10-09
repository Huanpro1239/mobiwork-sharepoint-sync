# Promotion Bonus: implementation and DMS evidence

Current source: `auto` resolves to `calc`. The dated records below describe the
investigation and earlier adapters, not the current default. Partial monetary and
quantity comparisons exist; complete customer listing and historical equivalence
to Paybonus remain unverified.

## Stability and cleanup (2026-10-07)

- Re-read paginated report windows at most three times when the source total changes;
  discard previous pass rows and retain completeness/identity gates.
- Reject missing identity and non-finite Bill values rather than silently using zero.
  Missing units block only lines belonging to a registered customer and programme SKU.
- Keep proposed rewards in `Thưởng dự kiến`; publish reward rows only for eligible
  customers. A missing display grading cannot create a `TRẢ THƯỞNG` line.
- Customer region/NPP uses unambiguous sales assignments from the department tree,
  with customer-region metadata taking precedence. Keep programme eligibility region
  separately as `Vùng áp dụng CT`; missing or conflicting assignments stay unknown.
- Share the existing CTKM blocking-issue classification. Diagnostic dry-runs retain
  `CanBoSung`; invalid identity/quantity/amount/conversion factor prevents production publication.
  Preserve the CTKM policy introduced in #102: missing pack mappings leave converted
  quantities blank, record `unit_gaps`, and do not block otherwise valid publication.
- Load template catalogues once per run instead of process-global mutable caching.
  Remove employee-specific exploratory probes; keep schema/count diagnostics.
- `quality_status` describes gaps independently of execution success;
  `dms_equivalence_verified=false` avoids presenting unit tests as financial reconciliation.

Read-only regression against the saved October Bill/catalogue snapshot: 2,108
sale lines, 98 catalogue entries (89 supported and 9 unsupported). One reward row
was eligible and 15 reached rows required display review. No source workbook was
modified and this older snapshot is not a live DMS reconciliation.

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

## Primary frontend evidence (public JavaScript)

Fetched verified HTTP-200 JavaScript from https://dms.mobiwork.vn/js/form/formPaybonusReport.js and https://dms.mobiwork.vn/js/form/global.js?v=17092026b. The Paybonus script getFilteroption reads .cal_sttt and sends sttt only when nonempty; it sends datepicker getDate().setHours(0,0,0,0) for both dates. Its report call is gCollector_ajax POST with arrCT and renders data.result. Frontend row fields explicitly include _id, _idCT, type, ma, ten, sdt, dc, loai, nhom, objThucHien, objChiTieu. Monetary target display uses per-program target definitions and so_tien index keys, with matching _idCT and min/max rules; this is not enough to map all mechanics/region fields without response fixtures.

The shared AJAX helper constructs Basic Authorization from web email and tokenkey cookies and sends x-alias. This is primary evidence for web-auth requirements; it is not evidence that the supplied OpenAPI userID/token are interchangeable. No cookie values are read or collected. User Chrome remains disconnected from browser-control tools. The UI builder supports an explicitly observed optional string sttt and omits it when empty exactly as the frontend; no numeric enum is guessed. Production remains gated until an authenticated customer response and complete region/target schema are validated.

## Full user-provided Chrome response (2026-10-06)

Parsed locally without committing the response. It contains 56 nested envelopes; 10 are nonempty and contain 85 record occurrences in total. Every envelope has RecordType=_Orders. This corrects earlier screenshot-only uncertainty: the response is not entirely empty, but neither the envelope count nor the 85 raw occurrences is a final customer/program/level report count. Nested rows have raw order data/repeatableData/settings, not the observed frontend final ma/ten/_idCT/objChiTieu schema. Do not zip 56 envelopes to the 57 selected IDs, deduplicate repeated order occurrences, or infer missing reward rows.

All query metadata inspected uses delivery date data.ngay_giao_hang.viewData from 1790787600000 through 1793465999999 inclusive (October 1 midnight to October 31 23:59:59.999 Vietnam). Thus this captured backend expands the end date to the full last day. Other observed predicates include order-type values 0/2, approved/sold/exported/delivered status labels, settings.toRejectCTTT != true, isDeleted=false, and per-envelope customer-ID membership. Customer IDs and raw order/contact values are not persisted in audit output. arrChiTieu=[] and arrTraThuong=[]; arrFormElement has 57 entries.

The diagnostic now supports --ui-response <local JSON path>, requiring no API credentials. It reports sanitized schema/counts/date spans, leaves customer_rows=null for unverified nested records, and does not enable workbook publication. Tested duplicate-envelope preservation and absence of customer ID/phone values in metadata. The fresh final DMS customer report and target/reward definitions are still necessary for 250-row financial equivalence.

## 2026-10-06 (afternoon): OpenAPI date probe and signed-in UI per-program run

### OpenAPI date bindings rejected
Run 37439905945 (audit, dry_run) probed all 57 dated-catalogue programs with four report date bindings: id_only, epoch_ui (startDate/endDate local-midnight epoch ms), epoch_full_day (endDate 23:59:59.999) and ddmmyyyy (fromdate/todate). Every variant returned 0 customer rows for 0 programs. Missing dates are therefore not the (sole) cause of empty OpenAPI results; the OpenAPI report remains unusable for this snapshot.

### sttt enum observed in the signed-in page
`.cal_sttt` is a hidden input set by `on_CaculateSTTT` (formReportCollectCommon.js) from `data-sttt` of the "Cách tính" menu:

| sttt | UI label |
|---|---|
| 0 | Tổng tiền (Đơn giá * Số lượng) — highlighted by default |
| 1 | Tổng tiền - chiết khấu SP |
| -1 | Tổng tiền - chiết khấu SP - CK đơn hàng |
| 2 | Tổng tiền có VAT (Đơn giá * Số lượng + VAT) |

On page load the hidden input is empty, so the default request omits sttt even though option 0 is highlighted.

### Per-program UI requests return final customer rows
The page's own `gCollector_ajax` POST `/PromotionBonusReport` (service host `serivceBussiness` = https://api.mobiwork.vn:3019, web Basic email:tokenkey + x-alias; session cookie lifetime `sessionOut`=30) was called with `arrCT` containing exactly ONE program and the page's October 1–31 filter. Body keys: only `arrCT`.

Results over the 57 programs in one pass: 9 programs returned final rows, 245 rows total; 27 returned nested `_Orders` envelopes; 21 returned empty arrays; 0 errors. Final row keys: `_idCT, soSuatCT, name, type, _id, ten, ma, sdt, dc, kv, npp, timepass, loai, nhom, objChiTieu, objTraThuong, objThucHien, arrDH, ckdh, ngay_dat_cuoi, ds_cuoi, time_soSuat, formElement, data_web`. `kv` is the customer region (Miền Trung 1B 229, blank 10, Miền Trung 1A 5, Miền Trung 2 1). Types seen: "" (203) and MUTI_SP_SL_SP (42). arrChiTieu keys: `_id, _idCT, ten, kh, min, max`. Golden workbook (captured earlier) has 250 rows; 245 at a later time is consistent but not yet a row-level match.

### Envelope responses are non-deterministic
Re-requesting a program that had just returned 210 final rows returned a single `_Orders` envelope with arrChiTieu=[]. Envelope responses must therefore be treated as an incomplete/transient server state and retried, never as "no customers". The 57-ID request observed earlier is the same envelope mode and must not be used.

### Operational caution
`gCollector_ajax` performs synchronous XHR; repeated runs froze the browser tab and DMS slowed noticeably. An automated client must call one program at a time, sequentially, with timeouts, bounded retries for envelopes and a pause between calls.

### Remaining decision before implementation
The working contract needs web-session credentials (email, tokenkey, alias) or a service-account login flow; OpenAPI token compatibility is unproven. Production export stays gated until the credential approach is chosen and a run matches the golden workbook at customer+program+level grain.

## Implementation: DMS web source (src/promotion_bonus_ui.py)

`promotion_bonus.py` now resolves `PROMOTION_BONUS_SOURCE` (default `auto`): with `MOBIWORK_WEB_EMAIL`, `MOBIWORK_WEB_TOKENKEY` and `MOBIWORK_WEB_ALIAS` present it uses the web source; otherwise the legacy OpenAPI path (and its `PROMOTION_BONUS_REQUIRE_DMS_MATCH` gate) is unchanged.

Web source behaviour:
- Programmes come from the dated OpenAPI catalogue (`fromdate`/`todate` = report month); optional `PROMOTION_BONUS_PROGRAM` comma list.
- One POST per programme, `arrCT=[id]`, sequential with a pause; envelope responses are retried with linear backoff (`PROMOTION_BONUS_UI_ATTEMPTS`=4, `..._BACKOFF_SECONDS`=20, `..._PAUSE_SECONDS`=3, `..._TIMEOUT_SECONDS`=180).
- Any programme still returning envelopes fails the run before publishing (override only with `PROMOTION_BONUS_ALLOW_PARTIAL=true`). HTTP 401/403 raises a token-renewal error.
- Workbook `BaoCaoTraThuong_Current.xlsx`: `Tong_hop` (one row per customer × rendered target cell), `Ket_qua` (reward products), `Kiem_tra` (period, sttt, per-programme status/attempts), plus one sheet per Khu vực (subdivisions preserved, blank → "Chưa xác định").
- Kế hoạch/Thực hiện/Còn lại/Tỷ lệ follow `renderData` for so_tien, MUTI_SP_SL_*, GR_* and default target types.

Setup: add the three web secrets (optionally `MOBIWORK_ORG_ID`) in GitHub → Settings → Secrets and variables → Actions, then run `MobiWork DMS Sync` with `report_scope=promotion_bonus_only`, `dry_run=true` first. The session token expires with the DMS web session (cookie lifetime 30 days); a 401/403 means it must be refreshed.

## 2026-10-07: Computed report from OpenAPI data (default source)

`PROMOTION_BONUS_SOURCE=auto` now resolves to `calc` (src/promotion_bonus_calc.py). No web session is needed.

Inputs: dated PromotionBonus catalogue (rules + registered `customer` IDs), Bill monthly master ChiTietSP, Customer catalogue (code, name, phone, address, type, group, province) and DisplayData for programmes linked to a display programme (`cttb`).

Rules verified against the DMS export `ReportPromotionBonus_ngo_cam_van_1791271808290.xlsx` (2026-10-06 14:30, 57 sheets, 258 rows) using Bill deliveries 01–05/10: 200 of 221 comparable customer rows matched exactly (amounts to the đồng, e.g. 7,064,081; quantities 72/144/324/1,080), 21 differed only because the customer's qualifying orders were delivered after the Bill snapshot; 0 rule mismatches.
1. registered customers only; 2. delivery date (Vietnam day) within the period; 3. sold lines only (`is_km` excluded); 4. a line counts only when its unit equals a unit declared for the SKU in the programme — DMS does not convert cases to bottles (40 cases + 60 bottles on a bottle programme = 60); 5. amount = `thanh_tien` (đơn giá × SL, sttt 0), quantity = `so_luong`; 6. reached when actual ≥ min and (max = 0 or actual < max); `BoiSo` multiplies the reward.

Open point: DMS does not list every registered customer (e.g. 581: 254 registered, 196 listed; 588: 23 registered, 1 listed while 5 others reached 144 bottles). DisplayData was joined (names matched case/space-insensitively: 8 of 15 required display programmes, 70 gradings in early October) but it is too sparse to explain the listing (581: 38 gradings vs 196 listed) and carries no explicit result field, so it does not exclude anyone. `Đủ điều kiện trả thưởng` is `Có` when reached and no display is required (or display says Đạt), `Cần kiểm tra trưng bày` when reached on a display programme without an Đạt grading, else `Không`. Run annotations print per-programme registered/sales/reached/display_ok/eligible counts to confirm the rule.

Output: `06_BaoCaoTraThuong/BaoCaoTraThuong_Current.xlsx` plus `06_BaoCaoTraThuong/YYYY/MM/BaoCaoTraThuong_YYYY-MM.xlsx`, refreshed by every scheduled sync. `Khu vực` is the programme's applicable region (`ctype`) because OpenAPI exposes no customer area.

## Past months (2026-10-07)

`PROMOTION_BONUS_MONTHS` (workflow input `bonus_months`, or `report_scope=promotion_history` which uses `all_existing`) recomputes past months from each month's Bill monthly master and the programme catalogue filtered to that month. Each month is written only to `06_BaoCaoTraThuong/YYYY/MM/BaoCaoTraThuong_YYYY-MM.xlsx`; `BaoCaoTraThuong_Current.xlsx` is never overwritten by a backfill, and run state goes to `_sync_state/promotion_bonus_history.json`. A month whose inputs are missing is skipped and reported, not fatal. Caveat: OpenAPI exposes the programme's current registered-customer list, not its history, so customers removed from a programme after a month closed are not shown for that month.

## Confirmed historical calculation periods (2026-10-08)

On 2026-10-08, the user explicitly confirmed that historical programmes
`013/TB/GT/01/2026`, `387/TB/GT/07/2026` and `391/TB/GT/07/2026` are calculated
separately each month. Their declarations are stored in `program_overrides` in
`config/promotion_detail.json`, independently of the Q4 declarations. This removes
the undecided-period hold for those programmes; display and other eligibility
conditions still apply. The configured cumulative summer cash programmes are unchanged.

## Confirmed sale units (2026-10-09)

On 2026-10-09, the user confirmed that both missing-unit sale lines in
`BH_104982026` are Chai: line 1, SKU `230100011`, quantity 1,680; line 2,
SKU `230100017`, quantity 960. Later the same day, the user explicitly confirmed
Chai as the default for both SKUs whenever the API leaves a sale's unit blank.
`sale_unit_defaults` stores that evidence and replaces the three redundant exact-line
declarations for these SKUs. It applies to both bonus qualification and invoice
conversion, including new orders. Source units retain priority, followed by an exact-line
override, then the confirmed SKU default. Gift units and other SKUs are not inferred.

## Template layout (2026-10-07)

`BaoCao` uses the user's "Báo cáo chi tiết CTKM theo KH" layout (config/promotion_detail_layout.json, title "BÁO CÁO TRẢ THƯỞNG CHI TIẾT THEO KHÁCH HÀNG - THÁNG MM/YYYY"). Rows are the Bill lines counted for each programme (Mã CTKM = programme code incl. level, e.g. `008/TB/GT/01/2026_Q4 - Mức 2`), rendered through promotion_detail.build_report so Vùng/Tỉnh/SS/NPP/Brand/Package and KÉT/THÙNG/BÌNH conversions match the CTKM report. Only eligible customers receive a `TRẢ THƯỞNG` line per reward. Unconfirmed display results, missing period inputs and undecided period methods retain proposed rewards but hold confirmation. Missing mappings go to `CanBoSung`.

## Two template views in one automated report (2026-10-09)

The user supplied the accumulation-customer tracking template and the 26-column CTKM
template. The Current/monthly bonus workbook now opens on `TheoDoiTichLuy`, followed by
`KhuyenMaiDonHang` styled with the same detail template. The tracking view keeps the first
20 fields of the supplied tracking template and expands month/attained-slot columns to
the selected programmes' date ranges, including years. It includes every registered
customer for each supported catalogue level, even without sales. The user's explicit
confirmation on October 9 defines one customer/level as one slot and leaves absent
registration dates blank. Programme-global `soSuat` is never substituted for a customer's slots.

Progress reads the existing cached Bill masters, sales only, exact rule SKU/unit matching,
with delivery-date periods. It reuses threshold, upper-bound, tier and BoiSo semantics.
Monthly programmes award threshold slots independently in each month; cumulative
programmes show increments of running attainment (including negative adjustments),
so summing monthly slots does not repeat a cumulative reward. Current eligibility and
cash/gift quantities remain owned by the existing calculator/ledger. Total accumulation
is informational across the programme; remaining is a non-positive shortfall for the
current calculation period, zero at the minimum target. It is not proof that display
or maximum-threshold conditions passed. The calculation mode, target unit, current
period actual and eligibility status are explicit beside the template fields.

Future/out-of-programme months are blank. A missing master or qualifying sale unit
leaves the affected month and aggregate totals unavailable, with a source reason;
other valid monthly results remain visible. Missing registration/contact fields stay
blank, not inferred from programme dates or customer names. Historical membership
and full Paybonus parity remain unverified. The second view keeps actual invoice
sales/gifts once each and pre-VAT amount in W, physical gift quantity in Z, without
mixing actual invoices with calculated reward lines. Existing supporting views and
legacy standalone CTKM exports remain compatible.

## Cash, gifts and invoice promotion coverage (2026-10-08)

`TraThuong` provides one row per registered customer × programme × reward, with
separate proposed/eligible cash amounts and physical quantities in the original
unit. Eligibility does not establish actual payment or gift delivery. `BaoCao`
column Z contains only physical gift quantity; cash reward lines use
`Tiền mặt trả thưởng (đ)`. Order allocations distinguish actual cash from the
estimated selling value of gifts. A gift without a source price leaves its value
unknown without hiding a known cash allocation. Existing AA/AB columns remain.

`KhuyenMaiDonHang` includes actual Bill promotions with the same month-of-order
filter, 26 template fields and conversion rules as the standalone CTKM report,
plus source line, unit, quantity and unmodified product discount for reconciliation.
Each sale is included once with all programme codes of the order; each gift is
included once with its direct programme. These transactions are not added again
to calculated rewards. Bonus eligibility still uses delivery dates.

`ChuongTrinh` retains every selected Bonus catalogue level, including unsupported
rules and programmes without registered customers or sales, followed by programmes
observed on Bill. Invoice coverage is not a catalogue of unobserved promotions.
The cached October 8 catalogue has 119 levels, including eight cash levels for
April–September, none applicable in October. It also contains one archived June
355 programme with two adjacent, disjoint quantity tiers over exactly the same purchase pool.
The calculator now selects the matching interval and applies that tier's reward
and the existing BoiSo rule once. Overlapping/gapped intervals or different purchase pools
remain unsupported, since their combination cannot be inferred safely. This is
derived from the catalogue structure and existing calculation rules, not a verified
Paybonus row comparison. Voucher products stay physical rewards; no cash value is
inferred from a product name. Current registration snapshots and missing historical
masters remain limitations; a successful workflow is not complete DMS equivalence.
