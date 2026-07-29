"""Create a Markdown follow-up report for a GitHub user's open pull requests."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


PR_FIELDS = "repository,number,title,updatedAt,url,commentsCount,labels,isDraft"


def parse_timestamp(value: str) -> datetime:
    """Parse the ISO-8601 timestamps returned by the GitHub CLI."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def authenticated_login() -> str:
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def fetch_open_prs(author: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "gh",
            "search",
            "prs",
            "--author",
            author,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            PR_FIELDS,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def age_in_days(updated_at: str, now: datetime) -> int:
    return max(0, (now - parse_timestamp(updated_at)).days)


def render_report(
    prs: Sequence[dict[str, Any]],
    *,
    author: str,
    stale_after_days: int,
    now: datetime,
) -> str:
    """Render a stable, readable report from `gh search prs` JSON."""
    ordered_prs = sorted(prs, key=lambda pr: pr["updatedAt"], reverse=True)
    fresh, stale = [], []
    for pr in ordered_prs:
        (stale if age_in_days(pr["updatedAt"], now) >= stale_after_days else fresh).append(pr)

    lines = [
        "# Open Pull Request Follow-up",
        "",
        f"Account: [`{author}`](https://github.com/{author})",
        f"Generated: {now.date().isoformat()} UTC",
        f"Open PRs: {len(ordered_prs)}",
        "",
    ]
    for heading, items, guidance in (
        ("Recent activity", fresh, "No follow-up is implied by this section."),
        (
            f"No activity for {stale_after_days}+ days",
            stale,
            "Review these before following up; inactivity alone does not mean a maintainer needs a reminder.",
        ),
    ):
        lines.extend([f"## {heading}", "", guidance, ""])
        if not items:
            lines.extend(["_None._", ""])
            continue
        for pr in items:
            repository = pr["repository"]["nameWithOwner"]
            age = age_in_days(pr["updatedAt"], now)
            draft = " (draft)" if pr.get("isDraft") else ""
            comments = pr.get("commentsCount", 0)
            lines.append(
                f"- [{repository}#{pr['number']}: {pr['title']}]({pr['url']}){draft} - "
                f"last activity {age}d ago; {comments} comment(s)"
            )
        lines.append("")
    return "\n".join(lines)


def load_prs(path: Path) -> list[dict[str, Any]]:
    # PowerShell commonly writes UTF-8 JSON with a BOM; accepting it keeps
    # offline reports portable across Windows and Unix shells.
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError("The JSON input must be an array from `gh search prs`.")
    return payload


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--author", help="GitHub login to report on (defaults to the authenticated account).")
    parser.add_argument("--stale-after-days", type=int, default=14, help="Days without activity before a PR is grouped as stale (default: 14).")
    parser.add_argument("--json-file", type=Path, help="Read previously saved `gh search prs` JSON instead of calling GitHub.")
    parser.add_argument("--output", type=Path, help="Write Markdown to this path instead of stdout.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.stale_after_days < 1:
        raise ValueError("--stale-after-days must be at least 1.")

    author = args.author or authenticated_login()
    prs = load_prs(args.json_file) if args.json_file else fetch_open_prs(author)
    report = render_report(prs, author=author, stale_after_days=args.stale_after_days, now=datetime.now(UTC))
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
