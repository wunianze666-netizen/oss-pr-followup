# OSS PR Follow-up

A dependency-free command-line helper for turning a GitHub user's open pull
requests into a short Markdown or JSON follow-up report. It is intended for
people who contribute to multiple projects and want to see which PRs have gone
quiet without maintaining a spreadsheet or remote database.

The default data source is GitHub's public REST API, so public accounts work
with Python alone. The tool is read-only: it never writes to GitHub and does not
inspect repository contents.

## Requirements

- Python 3.10+
- Internet access when reading live GitHub data

[GitHub CLI](https://cli.github.com/) is optional and only needed for the
`--source gh` mode.

## Install

Install directly from GitHub:

```bash
python -m pip install "git+https://github.com/wunianze666-netizen/oss-pr-followup.git"
```

For local development, install from a checkout:

```bash
python -m pip install .
```

## Usage

Generate a report for any public GitHub account:

```bash
oss-pr-followup --author octocat
```

Choose an account and mark PRs inactive for at least 21 days:

```bash
oss-pr-followup --author octocat --stale-after-days 21
```

Write the report to a local file:

```bash
oss-pr-followup --author octocat --output report.md
```

Produce structured output for another tool or dashboard:

```bash
oss-pr-followup --author octocat --format json --output report.json
```

Use `--limit` to control the maximum number included. Counts in the output are
explicitly labeled as the number of PRs in the report, not the account's total,
because a limit may truncate the search.

Unauthenticated API requests are suitable for occasional public reports. Set a
standard `GITHUB_TOKEN` environment variable for a higher rate limit and access
to PRs visible to that token:

```bash
GITHUB_TOKEN=... oss-pr-followup --author octocat
```

If `GITHUB_TOKEN` is set, `--author` may be omitted and the authenticated
account is detected automatically.

To use an existing GitHub CLI login instead:

```bash
oss-pr-followup --source gh
```

For an offline or reproducible report, save GitHub CLI output and pass it back
with the account name:

```bash
gh search prs --author octocat --state open --limit 100 \
  --json repository,number,title,updatedAt,url,commentsCount,labels,isDraft > prs.json
oss-pr-followup --author octocat --json-file prs.json
```

The offline input also accepts UTF-8 files with a byte-order mark, which is
useful when the JSON was saved from PowerShell on Windows.

## Report semantics

The report groups PRs by their most recent GitHub activity. "No activity" is
not the same as "maintainer action required"; users should read the discussion
and contribution guidelines before sending a follow-up. Draft PRs are marked
but remain in the report.

## Checks

```bash
python -m unittest discover -s tests -v
oss-pr-followup --help
```

## Privacy

The tool reads `GITHUB_TOKEN` only from the process environment and sends it
only to `api.github.com`; it does not print, store, or transmit the token
elsewhere. There is no telemetry or persistent account data. Generated reports
are ignored by default because PR titles and links may be private to the report
owner.

## License

MIT. See [LICENSE](LICENSE).
