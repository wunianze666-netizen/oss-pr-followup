# OSS PR Follow-up

A dependency-free command-line helper for turning a GitHub user's open pull
requests into a short Markdown follow-up report. It is intended for people who
contribute to multiple projects and want to see which PRs have gone quiet
without keeping a personal token or a remote database.

The tool reads public PR metadata through the authenticated `gh` CLI. It never
writes to GitHub and does not inspect repository contents.

## Requirements

- Python 3.10+
- [GitHub CLI](https://cli.github.com/) authenticated with `gh auth login`

## Usage

Generate a report for the authenticated GitHub account:

```bash
python src/oss_pr_followup.py
```

Choose an account and mark PRs inactive for at least 21 days:

```bash
python src/oss_pr_followup.py --author octocat --stale-after-days 21
```

Write the report to a local file:

```bash
python src/oss_pr_followup.py --output report.md
```

For an offline or reproducible report, save GitHub CLI output first and pass it
back with `--json-file`:

```bash
gh search prs --author octocat --state open --limit 100 \
  --json repository,number,title,updatedAt,url,commentsCount,labels,isDraft > prs.json
python src/oss_pr_followup.py --json-file prs.json
```

## Checks

```bash
python -m unittest discover -s tests -v
```

## Privacy

The repository contains no token handling, telemetry, or persistent account
data. Generated reports are ignored by default because PR titles and links may
be private to the report owner even when the source PRs are public.
