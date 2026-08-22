# Contributing

Thanks for helping improve MobiWork SharePoint Sync.

## Development setup

1. Use Python 3.12.
2. Create and activate a virtual environment.
3. Install development dependencies with `pip install -r requirements-dev.txt`.
4. Keep credentials and generated Excel files out of Git.

## Before opening a pull request

Run the same quality gates used by CI:

```bash
python -m compileall -q src tests
ruff check .
coverage run -m unittest discover -s tests -v
coverage report
```

Changes that touch data contracts, business keys, pagination, Excel generation, authentication, or SharePoint replacement semantics should include regression tests.

## Pull request scope

Prefer small, reviewable pull requests. Explain:

- the problem and root cause;
- the behavioral change;
- data-integrity or rollback implications;
- tests added or updated;
- any required deployment/configuration change.

Production code should fail closed on deterministic data-integrity errors. Do not weaken validation merely to make a workflow green.

## Sensitive information

Never commit MobiWork credentials, Microsoft tokens, customer data, exported business workbooks, authorization headers, tenant secrets, or private operational artifacts. Use `.env.example` only as a template and keep real values in environment variables or GitHub secrets.
