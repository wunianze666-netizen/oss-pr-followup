"""Create a follow-up report for a GitHub user's open pull requests."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PR_FIELDS = "repository,number,title,updatedAt,url,commentsCount,labels,isDraft"
API_ROOT = "https://api.github.com"
USER_AGENT = "oss-pr-followup/0.2.0"
AUTHOR_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
UTC = timezone.utc


class CLIError(RuntimeError):
    """An error that can be shown to a command-line user without a traceback."""


def validate_author(author: str) -> str:
    """Reject invalid logins before inserting them into a search query or URL."""
    if not AUTHOR_PATTERN.fullmatch(author):
        raise CLIError(f"`{author}` is not a valid GitHub account name.")
    return author


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp from GitHub."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def run_gh(arguments: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            ["gh", *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise CLIError("GitHub CLI was not found. Install `gh` or use the default API source.") from None
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or "the command failed"
        raise CLIError(f"GitHub CLI error: {detail}") from None
    return result.stdout


def authenticated_login_gh() -> str:
    login = run_gh(["api", "user", "--jq", ".login"]).strip()
    if not login:
        raise CLIError("GitHub CLI did not return an authenticated account.")
    return login


def api_request_json(
    url: str,
    *,
    token: str | None = None,
    opener: Any = urlopen,
) -> dict[str, Any]:
    """Read one GitHub API response without exposing credentials in errors."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(url, headers=headers)
    try:
        with opener(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8")).get("message", "request failed")
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = "request failed"
        remaining = error.headers.get("X-RateLimit-Remaining") if error.headers else None
        if error.code in (403, 429) and remaining == "0":
            raise CLIError(
                "GitHub API rate limit reached. Set GITHUB_TOKEN for a higher limit and try again."
            ) from None
        raise CLIError(f"GitHub API request failed ({error.code}): {detail}") from None
    except URLError as error:
        raise CLIError(f"Could not reach GitHub API: {error.reason}") from None
    except TimeoutError:
        raise CLIError("GitHub API request timed out.") from None

    if not isinstance(payload, dict):
        raise CLIError("GitHub API returned an unexpected response.")
    return payload


def authenticated_login_api(token: str | None) -> str:
    if not token:
        raise CLIError("Pass --author, or set GITHUB_TOKEN so the account can be detected.")
    login = api_request_json(f"{API_ROOT}/user", token=token).get("login")
    if not isinstance(login, str) or not login:
        raise CLIError("GitHub API did not return an authenticated account.")
    return login


def normalize_api_pr(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a Search Issues API item to the report's stable PR shape."""
    repository_url = item.get("repository_url")
    if not isinstance(repository_url, str):
        raise CLIError("GitHub API returned a pull request without a repository URL.")
    repository = "/".join(repository_url.rstrip("/").split("/")[-2:])
    if "/" not in repository:
        raise CLIError("GitHub API returned an invalid repository URL.")

    labels = [
        label.get("name")
        for label in item.get("labels", [])
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    ]
    return {
        "repository": {"nameWithOwner": repository},
        "number": item.get("number"),
        "title": item.get("title"),
        "updatedAt": item.get("updated_at"),
        "url": item.get("html_url"),
        "commentsCount": item.get("comments", 0),
        "labels": labels,
        "isDraft": bool(item.get("draft", False)),
    }


def fetch_open_prs_api(
    author: str,
    *,
    limit: int,
    token: str | None = None,
    request_json: Any = api_request_json,
) -> list[dict[str, Any]]:
    """Fetch up to `limit` open PRs through GitHub's public REST API."""
    results: list[dict[str, Any]] = []
    page = 1
    page_size = min(100, limit)
    while len(results) < limit:
        query = urlencode(
            {
                "q": f"is:pr is:open author:{author}",
                "sort": "updated",
                "order": "desc",
                "per_page": page_size,
                "page": page,
            }
        )
        payload = request_json(f"{API_ROOT}/search/issues?{query}", token=token)
        items = payload.get("items")
        if not isinstance(items, list):
            raise CLIError("GitHub API search response did not contain a pull request list.")
        results.extend(normalize_api_pr(item) for item in items if isinstance(item, dict))
        total_count = payload.get("total_count", len(results))
        if not isinstance(total_count, int):
            raise CLIError("GitHub API search response contained an invalid result count.")
        if not items or len(results) >= min(limit, total_count, 1000):
            break
        page += 1
    return results[:limit]


def fetch_open_prs_gh(author: str, *, limit: int) -> list[dict[str, Any]]:
    output = run_gh(
        [
            "search",
            "prs",
            "--author",
            author,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            PR_FIELDS,
        ]
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise CLIError(f"GitHub CLI returned invalid JSON: {error.msg}") from None
    if not isinstance(payload, list):
        raise CLIError("GitHub CLI returned an unexpected response.")
    return payload


def age_in_days(updated_at: str, now: datetime) -> int:
    return max(0, (now - parse_timestamp(updated_at)).days)


def validate_pr(pr: dict[str, Any]) -> None:
    required = {
        "repository": dict,
        "number": int,
        "title": str,
        "updatedAt": str,
        "url": str,
    }
    for field, expected_type in required.items():
        if not isinstance(pr.get(field), expected_type):
            raise CLIError(f"Pull request data has an invalid `{field}` field.")
    repository = pr["repository"].get("nameWithOwner")
    if not isinstance(repository, str) or "/" not in repository:
        raise CLIError("Pull request data has an invalid `repository.nameWithOwner` field.")
    try:
        parse_timestamp(pr["updatedAt"])
    except ValueError:
        raise CLIError("Pull request data has an invalid `updatedAt` timestamp.") from None


def report_pr(pr: dict[str, Any], now: datetime) -> dict[str, Any]:
    validate_pr(pr)
    labels = [
        label if isinstance(label, str) else label.get("name")
        for label in pr.get("labels", [])
        if isinstance(label, str) or isinstance(label, dict)
    ]
    return {
        "repository": pr["repository"]["nameWithOwner"],
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "updatedAt": pr["updatedAt"],
        "ageDays": age_in_days(pr["updatedAt"], now),
        "commentsCount": pr.get("commentsCount", 0),
        "isDraft": bool(pr.get("isDraft", False)),
        "labels": [label for label in labels if isinstance(label, str)],
    }


def build_report_data(
    prs: Sequence[dict[str, Any]],
    *,
    author: str,
    stale_after_days: int,
    now: datetime,
) -> dict[str, Any]:
    """Build a stable report model shared by Markdown and JSON output."""
    normalized = [report_pr(pr, now) for pr in prs]
    ordered_prs = sorted(normalized, key=lambda pr: pr["updatedAt"], reverse=True)
    recent, stale = [], []
    for pr in ordered_prs:
        (stale if pr["ageDays"] >= stale_after_days else recent).append(pr)
    return {
        "author": author,
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "staleAfterDays": stale_after_days,
        "pullRequestsInReport": len(ordered_prs),
        "recent": recent,
        "stale": stale,
    }


def render_markdown(data: dict[str, Any]) -> str:
    """Render a readable Markdown report from normalized report data."""
    author = data["author"]
    stale_after_days = data["staleAfterDays"]
    generated = parse_timestamp(data["generatedAt"])

    lines = [
        "# Open Pull Request Follow-up",
        "",
        f"Account: [`{author}`](https://github.com/{author})",
        f"Generated: {generated.date().isoformat()} UTC",
        f"PRs in report: {data['pullRequestsInReport']}",
        "",
    ]
    for heading, items, guidance in (
        ("Recent activity", data["recent"], "No follow-up is implied by this section."),
        (
            f"No activity for {stale_after_days}+ days",
            data["stale"],
            "Review these before following up; inactivity alone does not mean a maintainer needs a reminder.",
        ),
    ):
        lines.extend([f"## {heading}", "", guidance, ""])
        if not items:
            lines.extend(["_None._", ""])
            continue
        for pr in items:
            draft = " (draft)" if pr.get("isDraft") else ""
            comments = pr.get("commentsCount", 0)
            link_text = markdown_link_text(
                f"{pr['repository']}#{pr['number']}: {pr['title']}"
            )
            lines.append(
                f"- [{link_text}]({pr['url']}){draft} - "
                f"last activity {pr['ageDays']}d ago; {comments} comment(s)"
            )
        lines.append("")
    return "\n".join(lines)


def markdown_link_text(value: str) -> str:
    """Escape untrusted text used inside a Markdown link label."""
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def render_report(
    prs: Sequence[dict[str, Any]],
    *,
    author: str,
    stale_after_days: int,
    now: datetime,
) -> str:
    """Render a Markdown report from GitHub CLI-compatible PR data."""
    data = build_report_data(
        prs,
        author=author,
        stale_after_days=stale_after_days,
        now=now,
    )
    return render_markdown(data)


def load_prs(path: Path) -> list[dict[str, Any]]:
    # PowerShell commonly writes UTF-8 JSON with a BOM; accepting it keeps
    # offline reports portable across Windows and Unix shells.
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise CLIError("The JSON input must be an array from `gh search prs`.")
    return payload


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--author", help="GitHub login to report on.")
    parser.add_argument("--stale-after-days", type=int, default=14, help="Days without activity before a PR is grouped as stale (default: 14).")
    parser.add_argument("--json-file", type=Path, help="Read previously saved `gh search prs` JSON instead of calling GitHub.")
    parser.add_argument("--source", choices=("api", "gh"), default="api", help="GitHub data source (default: api).")
    parser.add_argument("--limit", type=int, default=100, help="Maximum open PRs to fetch, from 1 to 1000 (default: 100).")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown", help="Report format (default: markdown).")
    parser.add_argument("--output", type=Path, help="Write the report to this path instead of stdout.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        if args.stale_after_days < 1:
            raise CLIError("--stale-after-days must be at least 1.")
        if not 1 <= args.limit <= 1000:
            raise CLIError("--limit must be between 1 and 1000.")

        token = os.environ.get("GITHUB_TOKEN")
        if args.json_file:
            if not args.author:
                raise CLIError("--author is required with --json-file.")
            author = validate_author(args.author)
            prs = load_prs(args.json_file)
        elif args.source == "gh":
            author = validate_author(args.author or authenticated_login_gh())
            prs = fetch_open_prs_gh(author, limit=args.limit)
        else:
            author = validate_author(args.author or authenticated_login_api(token))
            prs = fetch_open_prs_api(author, limit=args.limit, token=token)

        data = build_report_data(
            prs,
            author=author,
            stale_after_days=args.stale_after_days,
            now=datetime.now(UTC),
        )
        report = (
            json.dumps(data, indent=2, ensure_ascii=False)
            if args.format == "json"
            else render_markdown(data)
        )
        if args.output:
            args.output.write_text(report + "\n", encoding="utf-8")
        else:
            print(report)
        return 0
    except (CLIError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
