## Problem

<!-- What is wrong or missing? Include the root cause when known. -->

## Change

<!-- What does this PR change? Keep the scope explicit. -->

## Data / operational impact

- [ ] No data-contract change
- [ ] No authentication/permission change
- [ ] No SharePoint storage/replacement change
- [ ] No schedule/concurrency change

Explain any unchecked item above.

## Validation

- [ ] `python -m compileall -q src tests`
- [ ] `ruff check .`
- [ ] `coverage run -m unittest discover -s tests -v`
- [ ] `coverage report`
- [ ] Regression test added/updated when behavior changed

## Rollout / rollback

<!-- Describe deployment requirements and how to recover if the change fails. -->
