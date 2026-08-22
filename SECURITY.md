# Security Policy

## Supported code

Security fixes should target the current `main` branch. This project is an integration pipeline and does not currently publish versioned releases.

## Reporting a vulnerability

Do not open a public issue containing credentials, tokens, tenant details, customer data, or a working exploit against a real deployment.

Report security problems privately to the repository owner through GitHub's private contact mechanisms available on the owner profile or repository. Include the affected component, impact, reproduction conditions, and a minimal redacted proof of concept where possible.

## Deployment security model

- MobiWork credentials belong in GitHub Actions secrets or local environment variables.
- Microsoft authentication uses short-lived credentials through GitHub OIDC / Microsoft Entra where deployed in Actions.
- Generated Excel files and audit output are excluded from Git by `.gitignore`.
- SharePoint writes use fail-closed verification and staged replacement for existing workbooks.
- Use least-privilege Microsoft Graph / SharePoint permissions for the target site and document library.

Before making a fork public, remove or replace deployment-specific tenant, site, library, and endpoint values that you do not intend to disclose.
