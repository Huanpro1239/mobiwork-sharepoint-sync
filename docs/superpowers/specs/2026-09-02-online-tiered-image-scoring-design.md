# Online Tiered Image Scoring Design

**Date:** 2026-09-02
**Status:** Draft for review
**Target:** `mobiwork-sharepoint-sync`
**Reference behavior:** `D:\DMS cham anh`

## 1. Objective

Bring the online SharePoint/GitHub Actions image KPI scoring behavior in line with the proven local DMS scorer without replacing the existing MobiWork, SharePoint, monthly KPI V2.4, checkpointing, or workbook workflows.

The change ports the reference project's TIER0–TIER4 decision cascade, keeps YOLO/OCR/face analysis as auditable evidence, protects human review labels, and prevents historical AI output from being presented as a current-policy result.

## 2. Current problem

The online repository still makes final decisions with the older conservative `decide_scores()` policy followed by `apply_detector_evidence()`. The reference DMS project instead makes the final decision with `decide_tiered_scores()`, which combines calibrated CLIP heads, nearest-reference consensus, detector/OCR evidence, fraud handling, and separate quality gates.

There is also an online-only data integrity issue. The production workflow configures `AI_LEGACY_SCORE_REMOTE`, and `cloud_sample_compat.py` reuses those historical rows by URL before downloading or rescoring the image. It copies `Phân Loại AI`, not an authoritative manual review label, then stamps the row with the active runtime signature. As a result, a policy code update alone cannot make online output consistent with the new policy.

A third gap is that `ClassificationResult` carries the pass quality gate but not the auto-fail quality gate, even though both classifier implementations calculate it. A tiered runtime would otherwise default missing auto-fail gate state to `True` and could emit an automatic failure that validation did not authorize.

## 3. Scope

### In scope

- Port the reference TIER0–TIER4 decision policy and its validated thresholds.
- Use the same final decision path in trained and prebuilt online classifiers.
- Propagate pass and auto-fail quality gates into every classification result.
- Preserve the three nearest reference records for consensus decisions and audit output.
- Keep the current cloud batch limits, runtime budget, resumable checkpoints, SharePoint publishing, KPI V2.4 formulas, and customer aggregation.
- Invalidate stale score caches when the policy changes.
- Disable normal production reuse of legacy AI labels while preserving human review labels already supported by the KPI workbook flow.
- Add unit, integration, workflow, and compatibility tests plus operator documentation.

### Out of scope

- Retraining CLIP heads or changing the pinned CLIP model/revision.
- Replacing the existing KPI V2.4 workbook schema and formulas.
- Changing MobiWork extraction, image synchronization, or SharePoint folder conventions.
- Treating YOLO/OCR/face detection as an independent classifier.
- Automatically converting an old AI decision into a human-approved decision.

## 4. Design principles

1. **One final policy:** CLIP/model code produces scores and neighbors; the tiered policy produces the final label.
2. **Evidence is supporting data:** detector/OCR evidence may support a pass or expose fraud, but cannot bypass novelty or model quality gates.
3. **Fail safely:** uncertain, out-of-domain, technically unavailable, or quality-gate-blocked cases become `Can_duyet` or `Khong_the_cham`, never an unsupported automatic failure.
4. **Reuse by content and policy:** automated results are reusable only when image SHA-256 and the complete model/policy signature match.
5. **Humans remain authoritative:** manual labels survive rescoring and are never overwritten by an AI migration shortcut.

## 5. Scoring architecture

For each available image:

1. The feature extractor produces the pinned CLIP embedding.
2. The classifier calculates:
   - sign-scene probability;
   - sign-pass and display-pass probabilities;
   - selected scene pass probability;
   - fraud probability;
   - maximum reference similarity;
   - three nearest reference records.
3. YOLO, OCR, and face detection produce `DetectorEvidence`:
   - signboard present;
   - brand/store keyword present;
   - bottle or pack present;
   - face present.
4. `decide_tiered_scores()` makes the final decision in this fixed order:
   - TIER0 fraud auto-fail or fraud review;
   - novelty review;
   - TIER1 high-confidence evidence/reference-backed pass;
   - TIER2 moderate-confidence evidence-backed or k-NN-consensus pass;
   - TIER3 clear fail;
   - TIER4 weighted pass/fail/review.
5. Every automatic pass is allowed only when the pass OOF quality gate passed. Every automatic fail is allowed only when the auto-fail OOF quality gate passed. A blocked automatic result becomes `REVIEW_QUALITY_GATE`.
6. The decision, underlying scores, nearest references, evidence, OCR text, quality-gate state, image SHA, and pipeline signature are written to the audit record.

The scene remains selected from the calibrated sign probability. The online-only scene resolver may remain for backward compatibility with old `REVIEW_SCENE` test doubles, but the real score-bearing path always re-evaluates through the tiered policy and cannot bypass fraud, novelty, or quality gates.

## 6. Tier behavior and thresholds

The initial online thresholds match the reference DMS project:

| Policy field | Value | Purpose |
|---|---:|---|
| `tier_fraud_auto_fail_min` | 0.85 | Strong fraud auto-fail |
| `tier_fraud_neighbor_auto_fail_min` | 0.70 | Fraud auto-fail when a nearest sample is “đối phó” |
| `tier_high_pass_min` | 0.80 | TIER1 high pass |
| `tier_high_pass_fraud_max` | 0.35 | Maximum fraud for TIER1 |
| `tier_evidence_pass_min` | 0.45 | TIER2 evidence/consensus pass |
| `tier_evidence_pass_fraud_max` | 0.50 | Maximum fraud for TIER2 |
| `tier_consensus_sim_min` | 0.75 | Minimum mean k-NN similarity for consensus |
| `tier_clear_fail_max` | 0.30 | TIER3 clear-fail pass ceiling |
| `tier_weighted_pass_min` | 0.40 | TIER4 weighted center |
| `tier_weighted_review_margin` | 0.10 | TIER4 explicit review band |

The existing novelty threshold and OOF quality-gate thresholds stay in force. Thresholds remain centralized in `scoring/config.py`, validated by `DecisionPolicy`, included in bundle metadata checks, and hashed into the runtime signature.

## 7. Classification result contract

`ClassificationResult` gains an explicit `auto_fail_gate_passed: bool` field. Both the trained classifier and `PrebuiltSceneClassifier` must populate it from `EvaluationReport.auto_fail_gate_passed`, including scene-resolution copies.

No production path may use `getattr(..., True)` for a real classifier result. A compatibility fallback is allowed only for deliberately minimal test doubles that do not expose model scores. This makes a missing safety field fail during development instead of silently authorizing automatic failures.

## 8. Bundle and cache compatibility

The tier policy changes final decisions but does not change embeddings or trained heads. The release therefore bumps the pipeline version and score-cache schema, causing a new runtime signature and invalidating prior AI score caches.

The currently deployed V2.3 prebuilt bundle may be accepted through a narrow, explicit compatibility check because rebuilding and uploading embeddings is not required for this policy-only change. Compatibility is allowed only when all of these are true:

- CLIP model ID and pinned revision match exactly;
- bundle pipeline/schema are the known V2.3.0/schema-4 pair;
- embeddings, heads, evaluation report, paths, and required metadata exist;
- every base threshold stored in the old bundle matches the corresponding current base threshold;
- the only absent threshold fields are the newly introduced tier fields;
- the runtime computes a new signature from the bundle bytes, new pipeline version, and full current policy.

Any other mismatch fails closed with a clear error. Newly built bundles must match the new pipeline, schema, complete thresholds, and implementation hash exactly. The broad `AI_ALLOW_LEGACY_PREBUILT_BUNDLE` escape hatch is not used as the production rollout mechanism.

## 9. Legacy results, checkpoints, and manual labels

The following data classes have different trust rules:

### Current score checkpoint

A remote score row is reusable only if it contains a valid payload, non-empty image SHA-256, and an exact current pipeline signature. The current SharePoint image store is append-only for a URL/date identity: synchronized filenames contain the URL digest and an existing non-empty identity is never overwritten. Under that invariant, the exact URL and runtime signature identify the same stored bytes whose SHA was recorded at scoring time. The normal local cache remains keyed by signature plus SHA-256. If image replacement is introduced later, checkpoint records must also store and verify SharePoint eTag/content hash before URL-based reuse is allowed.

### Historical AI export

`AI_LEGACY_SCORE_REMOTE` is disabled in the production workflow. Historical `Phân Loại AI` values are not reused by URL and are not inserted into the new signature cache. If an operator explicitly enables a migration-only compatibility mode, legacy rows must remain visibly marked as legacy and pending rescore; they cannot be counted as a current-policy result.

### Manual review label

Manual labels continue to come from the existing KPI workbook/manual-label merge. They remain authoritative across AI rescoring. Removing legacy AI reuse must not remove or overwrite `Nhãn Duyệt`, reviewer metadata, or existing workbook review decisions.

### Rollout effect

On the first production runs after deployment, prior AI-only rows become backlog. Existing bounded batches, runtime budget, checkpoints, and follow-up dispatch score that backlog progressively. Rows not yet rescored remain `PENDING_SCORE`; they are not converted to `Khong_dat` and do not silently use old AI labels.

## 10. KPI and error semantics

The image-level output keeps the existing Vietnamese labels:

- `Bien_hieu` and `Trung_bay`: accepted image classes;
- `Khong_dat`: automatic fail authorized by the fail quality gate;
- `Can_duyet`: ambiguous, novel, suspicious, or gate-blocked result;
- `Khong_the_cham`: technical inability to score.

KPI aggregation continues to use the existing manual-label override contract. `Can_duyet`, `PENDING_SCORE`, and `Khong_the_cham` must not be treated as automatic failure. No change is made to customer KPI formulas except where a regression test is needed to prove these semantics remain intact.

## 11. Observability

The production summary should distinguish:

- current-signature checkpoint hits;
- local SHA cache hits;
- newly scored unique images;
- pending backlog;
- manual labels preserved;
- legacy AI rows ignored or migration-marked;
- counts by tier status and technical/review status.

Logs and workbook audit columns must make it possible to determine why an image passed, failed, or entered review without rerunning the model.

## 12. Testing strategy

Implementation follows test-driven development.

1. **Pure policy tests** cover every tier, priority ordering, neighbor consensus, evidence behavior, novelty, the weighted review band, and both quality gates.
2. **Classifier contract tests** prove trained and prebuilt paths propagate `auto_fail_gate_passed` and preserve it when resolving/copying results.
3. **Image scoring integration tests** prove a score-bearing classification uses the tiered policy, while a minimal old test double still follows the compatibility path.
4. **Bundle tests** accept only the documented V2.3 policy-only compatibility case and reject model, revision, base-threshold, schema, or unknown metadata mismatches.
5. **Cloud compatibility tests** prove legacy AI reuse is off by default, current rows require SHA plus exact signature, and a legacy row cannot masquerade as a current result.
6. **Workflow tests** prove production does not configure unconditional legacy AI reuse and remains bounded/resumable.
7. **KPI regression tests** prove manual labels survive rescoring and review/pending/technical statuses do not become failures.
8. Run compile checks, Ruff, the application test suite, and the AI test suite before completion.

## 13. Deployment sequence

1. Merge code and tests with the production legacy reuse setting disabled.
2. Run a cloud dry run or bounded sample and inspect tier/status distribution, manual-label preservation, and technical failures.
3. Run the normal production workflow. Allow existing 500-image/runtime-bounded batches to checkpoint and continue until backlog reaches zero.
4. Compare online distributions and selected audit samples against the reference DMS result, with special attention to fraud, consensus pass, clear fail, and review cases.
5. Keep the prior published workbook and score exports recoverable during validation; rollback consists of redeploying the previous code/workflow, not rewriting manual labels.

## 14. Alternatives considered

### Copy the entire local DMS pipeline

Rejected because the online repository has newer SharePoint synchronization, KPI V2.4, customer-history, safe-export, and resumable cloud orchestration that should not be replaced.

### Change only `decision_policy.py`

Rejected because historical URL reuse would continue returning old decisions under the new runtime signature, and the missing auto-fail gate field would create a safety gap.

### Require an immediately rebuilt prebuilt bundle

Not required for the first rollout because the policy uses existing embeddings, heads, neighbors, and evaluation data. The narrow compatibility rule avoids an unnecessary asset migration while still failing closed on meaningful incompatibility. Future bundles use the new complete metadata contract.

### Keep legacy URL reuse for speed

Rejected for normal production because URL identity does not prove unchanged image content or current-policy evaluation. Bounded backlog processing provides a slower but auditable migration.

## 15. Acceptance criteria

- Online production emits the same TIER0–TIER4 status vocabulary and decision semantics as the reference DMS scorer.
- Both automatic pass and automatic fail decisions obey their corresponding OOF quality gate.
- Existing V2.3 bundle assets can be used only through the documented policy-only compatibility case.
- Old AI labels are not stamped with the new pipeline signature or counted as newly valid results.
- Manual review labels remain unchanged and authoritative.
- Current-score reuse requires exact signature plus image SHA-256.
- Pending, review, and technical-error rows do not become `Khong_dat` by default.
- Existing SharePoint publishing, bounded catch-up, KPI V2.4, and customer aggregation continue to pass regression tests.
- All compile, lint, application, and AI tests pass.
