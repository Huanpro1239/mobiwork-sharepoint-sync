# MobiWork → SharePoint → Image AI → Sales KPI

Production-oriented Python pipeline for **MobiWork DMS → Microsoft SharePoint → tiered Image Scoring → Sales KPI V2.4**.

The existing report/image sync remains lightweight and can run on GitHub-hosted workers. Heavy AI inference runs on a persistent Windows self-hosted runner labeled `dms-ai`.

## Architecture

```text
MobiWork Open API
   ├─ report sync ───────► SharePoint monthly masters
   └─ image sync ────────► SharePoint Data anh/YYYY-MM/...
                                  │
                                  ▼
                         Windows self-hosted runner
                                  │
                  ┌───────────────┴────────────────┐
                  ▼                                ▼
          Tiered Image Scoring               Sales KPI V2.4
        CLIP + YOLO + OCR               Visit/order M-1 + M
        evidence + quality gates        stock + image evidence
                  │                                │
                  │                     compact Customer History
                  │                     (1 row / Mã KH)
                  └───────────────┬────────────────┘
                                  ▼
                        Formula-driven Excel KPI
                                  ▼
                         SharePoint KPI/YYYY-MM/
```

## Key production controls

- SharePoint staged replacement and workbook semantic verification from the existing sync stack.
- New/Old classification uses compact `KPI/History/customer_history.csv`, one row per customer.
- The history file bootstraps historical monthly masters once, one workbook at a time; later KPI runs load only M-1/M workbook contents.
- M-1/M evidence is joined by `ma_kh`; KPI ownership is the employee visiting in M.
- `ma_phieu` is the primary order identity; legacy `dien_giai [...]` is only a fallback.
- Promotional `is_km=True` product rows are excluded from KPI volume/history facts.
- Image scores are cached by **model signature + SHA256 image bytes**.
- Prior M-1/M score CSV files can seed a fresh runner cache.
- Technical image failure is `Khong_the_cham`, never a business `Khong_dat`.
- Manual labels in `Chi_tiet_Anh_Checkin` survive re-export and drive live Excel recalculation.
- Production catch-up batches unique image URLs, re-scores unresolved legacy decisions, checkpoints progress and dispatches the next batch until no deferred/retryable URL remains.
- The workbook separates genuine manual review (`REVIEW_*` without column H) from technical failures and images still waiting for AI.
- Source failures retry at most three times by default, so one broken URL cannot stall publication forever.
- Model weights, references, DMS photos, secrets and KPI outputs never enter Git history.

## Repository layout

```text
src/
├─ mobiwork.py / sharepoint.py / image_sync.py   existing sync stack
├─ scoring/                                      Tiered image scoring
│  ├─ classifier.py / modeling.py
│  ├─ decision_policy.py / image_scoring.py
│  ├─ yolo_verifier.py / ocr_engine.py / face_detector.py
│  ├─ assets.py / score_cache.py / records.py
│  └─ ...
├─ kpi/                                          KPI V2.4
│  ├─ customer_aggregator.py
│  ├─ customer_history.py                        compact all-history customer master
│  ├─ kpi_rules.py
│  ├─ manual_labels.py / workbook_formulas.py
│  └─ kpi_exporter.py
├─ sharepoint_kpi_source.py
├─ score_kpi_pipeline.py
├─ run_score_kpi.py
└─ bootstrap_model_assets.py
```

## Tiered Image Scoring

The immutable V2.3 bundle supplies four calibrated heads (scene, sign validity,
display validity and fraud) plus the three nearest human-labelled references.
Production then resolves the result with the same TIER0–TIER4 cascade used by
the local reference project:

| Tier | Decision |
|---|---|
| TIER0 | strong fraud fail or suspicious-fraud review |
| novelty/scene | out-of-domain or unresolved scene review |
| TIER1 | high-confidence pass with physical/reference support |
| TIER2 | moderate pass with detector/OCR or two-reference consensus |
| TIER3 | clear low-score failure without relevant support |
| TIER4 | weighted pass/fail with an explicit review band |

Every automatic pass and automatic fail must pass its corresponding OOF quality
gate. YOLO/OCR/face output is supporting audit evidence and cannot bypass model,
novelty, fraud or quality-gate checks. Score caches are invalidated whenever the
complete decision-time runtime changes.

## Sales KPI V2.4

A customer is considered for month M only when there is a visit in M. Evidence can accumulate across M-1 + M:

- KHTC: at least one true order reaches the configured single-order KTB threshold (default 3.0).
- KHĐĐK: otherwise total M-1/M reaches the configured threshold (default 5.0 KTB).
- at least one `ghi_ton` in M-1/M;
- at least one effective `Bien_hieu` plus one `Trung_bay` in M-1/M;
- a valid note may replace sign evidence only, never display evidence;
- New/Old uses `first_activity_date` from the compact customer-history master.

For KPI month M:

```text
first_activity_date < first day of M  → Cũ
first_activity_date >= first day of M → Mới
missing first_activity_date           → Không rõ
```

The customer-history file lives at:

```text
KPI/History/customer_history.csv
```

On the first successful run, the engine bootstraps historical Visit/Order monthly masters one file at a time and writes one row per `ma_kh`. On later runs it only downloads M-1/M workbook contents and incrementally updates the history master without moving any earliest date forward. See `docs/CUSTOMER_HISTORY.md`.

Attendance and rewards are live Excel formulas, including Sunday-excluded workdays and reward caps. See `docs/KPI_RULES_V2_4.md`.

## Private AI assets

Private assets live in SharePoint, not Git:

```text
Model Assets/
├─ reference/...
├─ reference_overrides.csv
├─ weights/yolov8s-world.pt
└─ template/KPI_template.xlsx
```

One-time bootstrap from the previous local project:

```powershell
python src\bootstrap_model_assets.py --source "D:\DMS cham anh" --dry-run
python src\bootstrap_model_assets.py --source "D:\DMS cham anh"
```

## Run

Existing report sync:

```powershell
python src\run_all_reports.py
```

Rolling images:

```powershell
python src\run_images.py
```

AI + KPI:

```powershell
pip install -r requirements-ai.txt
python src\run_score_kpi.py
```

Historical month / no-upload validation:

```powershell
python src\run_score_kpi.py --period 2026-08 --dry-run
```

A dry run may bootstrap `runtime/output/customer_history.csv` locally, but does not publish the history master or KPI outputs to SharePoint.

## Outputs

```text
KPI/
├─ History/
│  └─ customer_history.csv
└─ YYYY-MM/
   ├─ Ket_qua_cham_cong_va_thuong_KPI.xlsx
   ├─ Ket_qua_Chi_tiet_Anh.csv
   └─ run_manifest.json
```

The monthly workbook is downloaded before re-export so existing `Nhãn Sửa Tay` can be preserved. The history master is uploaded only at the non-dry-run publish stage.

During catch-up, `scoring_checkpoint.csv` and `scoring_checkpoint_manifest.json` are transient files in the same monthly folder. The canonical workbook is left untouched while the manifest status is `warming_up`; the checkpoint is removed after a final `success` or `success_with_errors` publish. Terminal retry counts remain in the canonical `run_manifest.json`, preventing later scheduled runs with the same pipeline signature from restarting blocked URLs at attempt zero.

## Automation and quality gates

- `.github/workflows/mobiwork-sync.yml`: report masters.
- `.github/workflows/mobiwork-images.yml`: rolling image sync.
- `.github/workflows/image-scoring-kpi.yml`: AI + KPI on Windows `dms-ai`; also triggers after successful image sync.
- `.github/workflows/ci.yml`: compile, Ruff and coverage/unit tests without installing the heavy AI stack.

Local lightweight checks:

```bash
pip install -r requirements-dev.txt
python -m compileall -q src tests
ruff check .
coverage run -m unittest discover -s tests -v
coverage report
```

AI policy/model checks:

```bash
pip install -r requirements-ai.txt
python -m unittest discover -s tests_ai -v
```

## Security

Do not commit `.env`, Microsoft tokens, MobiWork credentials, customer exports, DMS images, reference photos, model weights, cache databases or generated KPI workbooks. Runtime/private assets are excluded by `.gitignore`.

See `SECURITY.md`, `docs/SELF_HOSTED_RUNNER.md` and `docs/CUSTOMER_HISTORY.md` before production enablement.
