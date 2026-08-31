# AI + KPI Production Runbook

## Purpose

This layer consumes the report masters and image files already persisted by the normal MobiWork/SharePoint workflows. It deliberately does not call MobiWork again during scoring.

## Trust boundaries

1. **SharePoint monthly masters** are the business source of truth for visits/orders.
2. **SharePoint `Data anh`** is the image source of truth for scoring.
3. **SharePoint `AI Assets`** stores internal model assets outside Git.
4. **Self-hosted runner cache** stores derived model bundle and SHA-based score cache.
5. **KPI workbook** is the human-review surface; manual labels are authoritative when present.

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
7. Download unique stored image files to memory.
8. Hash image bytes with SHA256.
9. Reuse cached score only when both SHA256 and model signature match.
10. Batch CLIP inference; isolate YOLO/OCR/face evidence per image.
11. Export image audit + customer KPI workbook.
12. If a previous monthly KPI workbook exists, load its manual labels before export.
13. Upload XLSX, detail CSV and JSON manifest to SharePoint.

## Failure behavior

- Missing/undecodable image → `Khong_the_cham`, never `Khong_dat`.
- Missing reference/weight/template → fail closed before scoring.
- Missing order/visit monthly master in configured history window → fail closed; do not silently fabricate history.
- Model/code/reference changes produce a different model signature and invalidate cached image decisions.
- `Can_duyet` never receives an artificial confidence score.
- Manual label conflicts fail closed during workbook export.

## Model safety

V2.3 preserves separate sign/display validity heads. When scene probability is ambiguous, detector/OCR evidence can select which validity head to evaluate, but the selected head must still pass reference novelty, fraud, validity threshold, and quality gates.

## History bootstrap

Accurate Mới/Cũ classification requires a complete activity history. Set:

```text
KPI_HISTORY_FROM_DATE=YYYY-MM-DD
```

only to a date from which all monthly `visit` and `order` masters are available. If omitted, the run starts at M-1 and the KPI engine surfaces a history-coverage warning.

## Change control

Before merging a scoring-policy change:

1. run lightweight CI;
2. run `tests_ai` on the self-hosted runner;
3. run reference OOF evaluation against the controlled reference dataset;
4. confirm auto-pass precision remains at or above the production quality gate;
5. perform a small real-image smoke run;
6. inspect workbook formulas/manual-label preservation;
7. merge only after those checks pass.
