# OSS PR Follow-up

[![Tests](https://github.com/wunianze666-netizen/oss-pr-followup/actions/workflows/tests.yml/badge.svg)](https://github.com/wunianze666-netizen/oss-pr-followup/actions/workflows/tests.yml)
[![Release](https://img.shields.io/github/v/release/wunianze666-netizen/oss-pr-followup)](https://github.com/wunianze666-netizen/oss-pr-followup/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A dependency-free CLI that turns one contributor's open pull requests into an
actionable Markdown or JSON report. It helps people contributing across many
repositories answer two questions:

1. Which pull requests need my attention?
2. Which pull requests are waiting on CI, review, or a maintainer?

The tool is read-only. It never comments, closes, labels, or modifies a pull
request.

## Why use it?

GitHub already provides excellent per-PR pages and a global pull request list.
OSS PR Follow-up adds a portable report that can be saved, reviewed offline, or
fed into another script.

The default activity report works for public accounts without authentication.
Optional triage mode uses one batched GraphQL query per page to add review, CI,
merge, and next-action signals.

## Install

Install the current release directly from GitHub:

```bash
python -m pip install "git+https://github.com/wunianze666-netizen/oss-pr-followup.git@v0.3.0"
```

Python 3.10 or later is required. GitHub CLI is optional.

## Activity report

Generate a report for any public GitHub account:

```bash
oss-pr-followup --author octocat
```

Change the inactivity threshold or write a JSON artifact:

```bash
oss-pr-followup --author octocat --stale-after-days 21
oss-pr-followup --author octocat --format json --output report.json
```

This mode uses GitHub's public REST API. Set `GH_TOKEN` or `GITHUB_TOKEN` for a
higher rate limit or access to pull requests visible to that token.

## Actionable triage

Triage mode groups pull requests into:

- **Author action needed**: requested changes, failed CI, merge conflicts, or a
  branch behind its base
- **Ready for maintainer**: approved, clean, and without a failing check
- **Waiting for CI**
- **Waiting for review**
- **Follow-up candidates**: inactive without a more specific workflow signal
- **Drafts**
- **Monitoring**

It requires a token because GitHub GraphQL does not support anonymous queries.
If GitHub CLI is already authenticated, inject its token only for the current
shell:

```bash
# bash/zsh
export GH_TOKEN="$(gh auth token)"
oss-pr-followup --author octocat --triage
```

```powershell
# PowerShell
$env:GH_TOKEN = gh auth token
oss-pr-followup --author octocat --triage
```

Use a least-privilege token. Public-only triage does not require write access.
Private pull requests appear only when the token can read their repositories.

Triage categories are evidence-based hints, not instructions to contact a
maintainer. Always read the pull request discussion and contribution policy
before following up.

## Other inputs

Use an existing GitHub CLI login instead of the default REST source:

```bash
oss-pr-followup --source gh
```

For an offline or reproducible activity report, save GitHub CLI output and pass
it back with the account name:

```bash
gh search prs --author octocat --state open --limit 100 \
  --json repository,number,title,updatedAt,url,commentsCount,labels,isDraft > prs.json
oss-pr-followup --author octocat --json-file prs.json
```

The offline input accepts UTF-8 files with or without a byte-order mark.

## Project direction

This project is not a fork and does not copy another dashboard's source. See
[the product research](docs/product-research.md) for comparable tools,
licenses, ideas considered, and the deliberate differences in scope.

## Development

```bash
python -m pip install .
python -m compileall -q src tests
python -m unittest discover -s tests -v
oss-pr-followup --help
```

CI runs the package and test suite on Linux and Windows with Python 3.10 and
3.13.

## Privacy

The tool reads `GH_TOKEN` or `GITHUB_TOKEN` only from the process environment
and sends it only to `api.github.com`. It does not print, persist, or transmit
the token elsewhere. There is no telemetry, remote database, or stored account
data. Generated `report.md` files are ignored by default because private PR
titles and links may appear in authenticated reports.

See [SECURITY.md](SECURITY.md) for private vulnerability reporting and token
handling guidance.

## License

MIT. See [LICENSE](LICENSE).
