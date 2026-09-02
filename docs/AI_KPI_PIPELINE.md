# AI + KPI Production Runbook

## Purpose

This layer consumes the report masters and image files already persisted by the normal MobiWork/SharePoint workflows. It deliberately does not call MobiWork again during scoring.

## Trust boundaries

1. **SharePoint monthly masters** are the business source of truth for visits/orders.
2. **SharePoint `Data anh`** is the image source of truth for scoring.
3. **SharePoint `AI Assets`** stores internal model assets outside Git.
4. **Runner cache** stores derived model artifacts and SHA-based score cache.
5. **KPI workbook** is the human-review surface; manual labels are authoritative when present. Only current `REVIEW_*` decisions without a manual label belong to the manual queue.

## Required SharePoint structure

```text
AI Assets/
  reference/
    Dat/Bien hieu/
    Dat/Trung bay/
    Khong Dat/Khong dat bien hieu/
    Khong Dat/Khong dat trung bay/
    Khong Dat/doi pho/
    reference_overrides.csv       # optional
  weights/yolov8s-world.pt
  template/KPI_template.xlsx

Data anh/YYYY-MM/<employee>/<customer>/<image>
KPI/YYYY-MM/<outputs>
```

## Self-hosted runner

Recommended labels:

```text
self-hosted
Windows
X64
dms-ai
```

The runner should be persistent so Hugging Face/model caches and `runtime/cache/image_scores.sqlite3` survive between runs.

For GPU use, install the correct NVIDIA driver/CUDA-compatible PyTorch package for the host. The application automatically selects CUDA when `torch.cuda.is_available()` is true.

## Execution sequence

1. Resolve SharePoint drive.
2. Sync changed reference/weight/template assets by `eTag` + size.
3. Read visit and order monthly masters from SharePoint.
4. Flatten `ChiTietSP` for order KPI facts.
5. Aggregate customer KPI facts.
6. Locate each rolling M-1/M image already stored under `Data anh`.
7. Group unresolved rows by exact image URL. Try the stored path of each occurrence until one copy loads, score its bytes once, then fan the result back to every occurrence.
8. Hash image bytes with SHA256.
9. Reuse a cached score only when both SHA256 and the **complete scoring-runtime signature** match. The runtime signature includes the trained-model signature plus production evidence/OCR/detector/quality logic, so policy-code changes cannot silently reuse stale scores.
10. Batch CLIP inference; isolate YOLO/OCR/face evidence per image.
11. Re-score legacy AI rows through the current precision policy. Legacy AI auto-finals are not considered current decisions after a scoring-policy change. Human labels are preserved separately from AI legacy state and remain authoritative.
12. Export image audit + customer KPI workbook.
13. If a previous monthly KPI workbook exists, load its manual labels before export. Re-scoring never clears column H.
14. When a bounded production backlog remains, upload a checkpoint and dispatch the next batch automatically.
15. Upload the canonical XLSX, detail CSV and JSON manifest only after the pending/retryable queue reaches zero, then remove the transient checkpoint.

## Queue and review states

- `REVIEW_*` with no valid manual label → section **2A**, genuinely waiting for human review.
- `REVIEW_*` with a valid manual label → resolved; preserved in column H and excluded from section 2A.
- `TECHNICAL_FAILURE` → section **2B**, never counted as manual review.
- `PENDING_SCORE` → section **2C**, never counted as manual review.
- `AUTO_*` → final model decision; no manual action required.

Production batching counts **unique URLs**, not workbook rows. Never-attempted URLs are processed before retries. `AI_PRODUCTION_MAX_TECHNICAL_RETRIES` defaults to `3`; attempt counts are merged from both the transient checkpoint manifest and the canonical run manifest. After that limit the URL is published as an explicit blocked technical error and remains blocked for the same pipeline signature, so later scheduled runs cannot restart an endless retry cycle.

## Failure behavior

- Missing/undecodable image → `Khong_the_cham`, never `Khong_dat`; it is retried finitely and remains a technical state rather than becoming manual review.
- Missing reference/weight/template → fail closed before scoring.
- Missing order/visit monthly master in configured history window → fail closed; do not silently fabricate history.
- Trained-model, reference, detector, OCR, quality or production evidence changes produce a different scoring-runtime signature and invalidate cached image decisions.
- `Can_duyet` never receives an artificial confidence score.
- Manual label conflicts fail closed during workbook export.
- A production manifest is `warming_up` only while deferred or retryable URLs remain. It is `success_with_errors` when scoring is complete but blocked technical URLs remain; manual-review rows do not block canonical publishing.

## Model safety

The validated V2.3 CLIP/logistic bundle remains immutable unless it is deliberately retrained and OOF-validated. A separate V2.4 production evidence layer is precision-first:

- sign auto-pass requires a non-trivial learned pass score plus sign/store evidence or close positive human-reference consensus; Vikoda/Đảnh Thạnh OCR is useful support but is not mandatory for valid generic store signs;
- generic store text such as `tạp hóa`, `cửa hàng`, `đại lý` supports scene/store evidence but is not recorded as a Vikoda brand hit;
- display auto-pass requires bottle/pack evidence plus stricter model/reference support;
- severely small, dark, bright or blurry images are downgraded to review instead of receiving an automatic business pass/fail;
- uncertain evidence is sent to `Can_duyet`; the system prefers lower auto-coverage over a false automatic pass.

When scene probability is ambiguous, detector/OCR evidence can select which V2.3 validity head to evaluate, but the selected head must still pass reference novelty, fraud, validity threshold, model quality gates and the production evidence layer.

## History bootstrap

Accurate Mới/Cũ classification requires a complete activity history. Set:

```text
KPI_HISTORY_FROM_DATE=YYYY-MM-DD
```

only to a date from which all monthly `visit` and `order` masters are available. If omitted, the run starts at M-1 and the KPI engine surfaces a history-coverage warning.

## Change control

Before merging a scoring-policy change:

1. run lightweight CI;
2. run scoring-policy regression tests;
3. preserve compatibility with the validated prebuilt model bundle unless a retrain is intentional;
4. confirm automatic decisions are precision-first and stale cache signatures cannot be reused;
5. perform a small real-image smoke run in trusted main context;
6. inspect workbook formulas/manual-label preservation;
7. merge production scoring changes only after these checks pass.
