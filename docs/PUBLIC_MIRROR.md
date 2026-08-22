# Public Mirror Strategy

The private repository remains the production source of truth. The public repository is a sanitized downstream mirror used for open-source discovery, stars, forks, issues, and community contributions.

## Repository roles

### Private production repository

`Huanpro1239/mobiwork-sharepoint-sync`

Keep here:

- company SharePoint host/site/library targets;
- production schedules and concurrency controls;
- GitHub OIDC deployment settings;
- active `config/reports.json`;
- operational runbooks;
- production-only fixes and validation evidence.

The hourly and 09:00 company sync continues to run only from this repository.

### Public open-source repository

Recommended name: `Huanpro1239/mobiwork-sharepoint-etl`

Publish only:

- reusable Python ETL engine;
- tests and CI;
- disabled example report mappings;
- generic `.env.example`;
- architecture, security and contribution docs;
- MIT license.

Do not publish the private production workflow, active report mapping, tenant identifiers, operational runbook, secrets, generated workbooks, or audit data.

## Build the public mirror

Run the manual GitHub Actions workflow **Build Sanitized Public Mirror**. It:

1. copies only an explicit allowlist;
2. replaces the active report configuration with the disabled example;
3. omits the production sync workflow and operations runbook;
4. removes private SharePoint defaults from the public copy of `src/sharepoint.py`;
5. rewrites the README for the public repository;
6. scans the result for known internal deployment markers;
7. runs compile, Ruff, unit tests, and coverage inside the generated mirror;
8. uploads `mobiwork-sharepoint-etl.zip` as a short-lived workflow artifact.

The private production code is not modified by this export process.

## Promotion direction

Use one direction for automated publication:

```text
private production -> sanitized public mirror
```

Do not automatically deploy public `main` into the company environment.

For useful community changes, review the public pull request first, then intentionally port/cherry-pick the change into a private production PR and let the private CI/production validation gates decide whether it is safe to deploy.

## Public launch checklist

After creating the empty public repository and pushing the sanitized mirror:

- set the description to: `Production-grade MobiWork DMS to Excel and SharePoint ETL with Python, Microsoft Graph and GitHub Actions.`;
- add topics: `mobiwork`, `sharepoint`, `microsoft-graph`, `excel`, `etl`, `python`, `github-actions`, `data-pipeline`, `dms`, `microsoft-365`;
- enable Issues and Discussions if you want community support/contributions;
- create a `v1.0.0` release with a concise problem/solution summary;
- pin the public repository on the GitHub profile;
- publish a short demo using synthetic/sample data only;
- link the repository from relevant technical posts or communities without using artificial stars or engagement automation.

GitHub does not publish a guaranteed formula for Trending. Public visibility, legitimate recent star activity, useful documentation, clear topics, releases, and real community engagement improve discoverability, but Trending cannot be guaranteed.
