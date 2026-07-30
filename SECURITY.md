# Security Policy

OSS PR Follow-up handles GitHub tokens in optional authenticated modes. It does
not need write permission and should never persist a token.

## Report a vulnerability

Do not open a public issue if a report contains a token, private repository
name, private pull request title, or reproducible credential exposure.

Use the repository's
[private security advisory form](https://github.com/wunianze666-netizen/oss-pr-followup/security/advisories/new)
instead. Include the affected version, operating system, reproduction steps,
and the smallest safe example needed to demonstrate the problem.

## Token guidance

- Prefer a short-lived, least-privilege token.
- Use `GH_TOKEN` or `GITHUB_TOKEN` in the process environment.
- Do not place tokens in command arguments, JSON fixtures, generated reports,
  screenshots, issues, or pull requests.
- Revoke the token immediately if it appears in terminal history or a public
  artifact.

The application sends authenticated requests only to `api.github.com`. It has
no telemetry, backend, or credential store.

## Supported versions

Security fixes are applied to the latest release. Reproduce a report with the
latest version before submitting it when possible.
