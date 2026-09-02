# Fix Online Scoring Runtime Plan

## Goal

Repair the current `main` scoring runtime after integrating the latest GitHub changes, while preserving its newly added human-reference tiered policy and bounded production catch-up.

## Tasks

1. Add regression tests proving that the auto-fail OOF gate belongs to each `ClassificationResult` and that a blocked fail gate downgrades a tiered automatic failure to review.
2. Extend `ClassificationResult` and every trained/prebuilt constructor and scene-resolution copy with `auto_fail_gate_passed`.
3. Make `image_scoring` read both quality gates from the resolved classification result and default a missing auto-fail gate to false for score-bearing compatibility objects.
4. Add workflow regression tests proving production and probe workflows no longer configure `AI_LEGACY_SCORE_REMOTE`.
5. Remove the obsolete legacy CSV environment dependency from cloud workflows. The current queue already requires every legacy AI result to be rescored, so downloading this CSV can only add a failure point.
6. Update operator documentation to describe the tiered runtime, legacy rescore behavior, and gate propagation.
7. Run compile, Ruff, all application tests, and all AI tests. Review the final diff for accidental workflow or KPI changes.

## Non-goals

- No model retraining or bundle replacement.
- No SharePoint schema or KPI formula change.
- No change to bounded queue size, retry policy, or workflow dispatch behavior.
- No broad checkpoint redesign in this patch; current checkpoint reuse relies on the image store's immutable URL-digest identity and exact runtime signature.
