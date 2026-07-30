# Product Research

This document records the public products reviewed before the v0.3 triage
design. It exists to keep the project's direction explainable and to separate
product research from source-code reuse.

Research snapshot: 2026-07-30. Star counts are approximate and will change.

## Comparable projects

| Project | Approx. stars | License | Primary job |
| --- | ---: | --- | --- |
| [GitHub pull request dashboard](https://github.com/pulls) | N/A | Proprietary service | Search and filter pull requests involving the signed-in user |
| [dlvhdr/gh-dash](https://github.com/dlvhdr/gh-dash) | 12,180 | MIT | Interactive, configurable terminal UI for PRs and issues |
| [octobox/octobox](https://github.com/octobox/octobox) | 4,476 | AGPL-3.0 | Notification inbox with archive, filters, and enriched status |
| [gitify-app/gitify](https://github.com/gitify-app/gitify) | 5,299 | MIT | Cross-platform desktop notification client |
| [AKharytonchyk/git-pull-request-dashboard](https://github.com/AKharytonchyk/git-pull-request-dashboard) | 10 | MIT | Browser dashboard across selected repositories |
| [jammutkarsh/pr-pulse](https://github.com/jammutkarsh/pr-pulse) | 6 | MIT | Browser extension for PR status and navigation |
| [rishiip/pr-dashboard](https://github.com/rishiip/pr-dashboard) | 1 | MIT | Single-file browser dashboard with attention signals |

None of these repositories is an upstream of OSS PR Follow-up. This repository
is not a fork and has no imported commit history.

## What the research validated

Successful tools consistently make workflow state visible rather than showing
only a chronological list:

- Octobox distinguishes unfinished notifications from archived work and
  restores items when new activity arrives.
- gh-dash lets users define the filters and sections that match their workflow.
- Gitify emphasizes local, cross-platform access and focused filtering.
- Lightweight PR dashboards surface review, CI, draft, and merge signals.

These are product observations, not copied implementations.

## Deliberate differentiation

OSS PR Follow-up focuses on external contributors who author pull requests
across unrelated repositories. It intentionally remains:

- read-only, with no automated comments or maintainer-facing mutations;
- usable for public activity reports without a token;
- dependency-free at runtime;
- batch-oriented rather than an always-running server, TUI, or desktop app;
- exportable as stable Markdown and JSON;
- conservative about follow-up recommendations.

The optional triage mode uses GitHub's GraphQL API to fetch review, CI, merge,
and review-request signals in batches. Its category rules were implemented in
this repository and are covered by local tests.

## License and attribution boundary

No source files, assets, tests, documentation passages, or commit history were
copied from the projects above. Reviewing a public product's behavior and
designing an independent implementation around public GitHub APIs does not
incorporate its licensed code.

If future work incorporates code from another project, the change must document
the source, license, copyright notice, and compatibility analysis before it is
merged.

## Product standard

New features should satisfy all of the following:

1. Solve a repeated contributor workflow problem.
2. Preserve read-only behavior by default.
3. Avoid unnecessary API calls and explain authentication requirements.
4. Produce deterministic machine-readable output.
5. Include tests for classification and failure behavior.
6. Be validated against real public pull requests without publishing private
   report data.
