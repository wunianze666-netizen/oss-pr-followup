"""Create a follow-up report for a GitHub user's open pull requests."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PR_FIELDS = "repository,number,title,updatedAt,url,commentsCount,labels,isDraft"
API_ROOT = "https://api.github.com"
GRAPHQL_URL = f"{API_ROOT}/graphql"
GRAPHQL_PAGE_SIZE = 10
HTTP_MAX_ATTEMPTS = 3
HTTP_RETRY_DELAY_CAP = 10.0
HTTP_TRANSIENT_STATUSES = frozenset({502, 503, 504})
VERSION = "0.3.0"
USER_AGENT = f"oss-pr-followup/{VERSION}"
AUTHOR_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
UTC = timezone.utc
TRIAGE_QUERY = """
query OpenPullRequestTriage($query: String!, $first: Int!, $after: String) {
  search(query: $query, type: ISSUE, first: $first, after: $after) {
    issueCount
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on PullRequest {
        repository {
          nameWithOwner
        }
        number
        title
        updatedAt
        url
        isDraft
        author {
          login
        }
        comments {
          totalCount
        }
        labels(first: 20) {
          nodes {
            name
          }
        }
        reviewDecision
        reviewRequests(first: 1) {
          totalCount
        }
        mergeStateStatus
        reviewThreads(last: 10) {
          totalCount
          nodes {
            isResolved
            isOutdated
            comments(last: 1) {
              nodes {
                author {
                  login
                  __typename
                }
              }
            }
          }
        }
        commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                state
              }
            }
          }
        }
      }
    }
  }
}
"""
TRIAGE_SECTIONS = (
    (
        "author-action",
        "Author action needed",
        "Address these before waiting for another maintainer response.",
    ),
    (
        "ready-for-maintainer",
        "Ready for maintainer",
        "Approval and checks look ready; repository policy still determines whether the PR can merge.",
    ),
    (
        "waiting-ci",
        "Waiting for CI",
        "Checks are still running; no follow-up is implied.",
    ),
    (
        "waiting-review",
        "Waiting for review",
        "A review is required or has been requested.",
    ),
    (
        "follow-up-candidate",
        "Follow-up candidates",
        "Read the discussion and contribution policy before sending a reminder.",
    ),
    (
        "draft",
        "Drafts",
        "Draft pull requests are normally waiting for author work.",
    ),
    (
        "monitoring",
        "Monitoring",
        "No immediate action signal was detected.",
    ),
)


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


def retry_after_seconds(error: HTTPError) -> float | None:
    """Return a bounded numeric Retry-After delay when the server supplied one."""
    value = error.headers.get("Retry-After") if error.headers else None
    if value is None:
        return None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return None
    if delay < 0 or delay > HTTP_RETRY_DELAY_CAP:
        return None
    return delay


def read_json_request(
    request: Request,
    *,
    opener: Any = urlopen,
    sleeper: Any = time.sleep,
) -> dict[str, Any]:
    """Execute a GitHub request without exposing credentials in errors."""
    for attempt in range(HTTP_MAX_ATTEMPTS):
        try:
            with opener(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8")).get(
                    "message", "request failed"
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = "request failed"
            remaining = (
                error.headers.get("X-RateLimit-Remaining") if error.headers else None
            )
            if error.code in (403, 429) and remaining == "0":
                raise CLIError(
                    "GitHub API rate limit reached. Set GH_TOKEN or GITHUB_TOKEN and try again."
                ) from None
            retry_after_header = error.headers.get("Retry-After") if error.headers else None
            retry_after = retry_after_seconds(error)
            retryable = error.code in HTTP_TRANSIENT_STATUSES or (
                error.code in (403, 429) and retry_after is not None
            )
            bounded_delay = retry_after_header is None or retry_after is not None
            if retryable and bounded_delay and attempt + 1 < HTTP_MAX_ATTEMPTS:
                sleeper(retry_after if retry_after is not None else 2.0**attempt)
                continue
            raise CLIError(f"GitHub API request failed ({error.code}): {detail}") from None
        except URLError as error:
            if attempt + 1 < HTTP_MAX_ATTEMPTS:
                sleeper(2.0**attempt)
                continue
            raise CLIError(f"Could not reach GitHub API: {error.reason}") from None
        except TimeoutError:
            if attempt + 1 < HTTP_MAX_ATTEMPTS:
                sleeper(2.0**attempt)
                continue
            raise CLIError("GitHub API request timed out.") from None

    if not isinstance(payload, dict):
        raise CLIError("GitHub API returned an unexpected response.")
    return payload


def github_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def api_request_json(
    url: str,
    *,
    token: str | None = None,
    opener: Any = urlopen,
) -> dict[str, Any]:
    """Read one GitHub REST API response."""
    request = Request(url, headers=github_headers(token))
    return read_json_request(request, opener=opener)


def graphql_request_json(
    query: str,
    variables: dict[str, Any],
    *,
    token: str,
    opener: Any = urlopen,
) -> dict[str, Any]:
    """Run an authenticated GitHub GraphQL query."""
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = Request(
        GRAPHQL_URL,
        data=body,
        headers=github_headers(token),
        method="POST",
    )
    payload = read_json_request(request, opener=opener)
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        messages = [
            error.get("message")
            for error in errors
            if isinstance(error, dict) and isinstance(error.get("message"), str)
        ]
        detail = "; ".join(messages) if messages else "query failed"
        raise CLIError(f"GitHub GraphQL query failed: {detail}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CLIError("GitHub GraphQL response did not contain data.")
    return data


def environment_token() -> str | None:
    """Use the same token environment variables recognized by GitHub tooling."""
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def authenticated_login_api(token: str | None) -> str:
    if not token:
        raise CLIError("Pass --author, or set GH_TOKEN/GITHUB_TOKEN so the account can be detected.")
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


def normalize_graphql_pr(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a GraphQL PullRequest node to the report's stable PR shape."""
    repository = item.get("repository")
    if not isinstance(repository, dict):
        raise CLIError("GitHub GraphQL returned a pull request without a repository.")

    label_connection = item.get("labels")
    label_nodes = label_connection.get("nodes", []) if isinstance(label_connection, dict) else []
    labels = [
        label.get("name")
        for label in label_nodes
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    ]

    comments = item.get("comments")
    comments_count = comments.get("totalCount", 0) if isinstance(comments, dict) else 0
    review_requests = item.get("reviewRequests")
    review_request_count = (
        review_requests.get("totalCount", 0) if isinstance(review_requests, dict) else 0
    )

    author = item.get("author")
    author_login = author.get("login") if isinstance(author, dict) else None
    review_threads = item.get("reviewThreads")
    thread_nodes = review_threads.get("nodes", []) if isinstance(review_threads, dict) else []
    thread_total = (
        review_threads.get("totalCount", len(thread_nodes))
        if isinstance(review_threads, dict)
        else 0
    )
    active_thread_count = 0
    author_action_thread_count = 0
    reviewer_action_thread_count = 0
    for thread in thread_nodes:
        if (
            not isinstance(thread, dict)
            or thread.get("isResolved")
            or thread.get("isOutdated")
        ):
            continue
        active_thread_count += 1
        thread_comments = thread.get("comments")
        comment_nodes = (
            thread_comments.get("nodes", []) if isinstance(thread_comments, dict) else []
        )
        latest_comment = comment_nodes[-1] if comment_nodes else None
        latest_author = (
            latest_comment.get("author") if isinstance(latest_comment, dict) else None
        )
        if not isinstance(latest_author, dict) or latest_author.get("__typename") == "Bot":
            continue
        latest_login = latest_author.get("login")
        if not isinstance(latest_login, str) or not latest_login:
            continue
        if isinstance(author_login, str) and latest_login.casefold() == author_login.casefold():
            reviewer_action_thread_count += 1
        else:
            author_action_thread_count += 1

    ci_status = None
    commits = item.get("commits")
    commit_nodes = commits.get("nodes", []) if isinstance(commits, dict) else []
    if commit_nodes and isinstance(commit_nodes[0], dict):
        commit = commit_nodes[0].get("commit")
        rollup = commit.get("statusCheckRollup") if isinstance(commit, dict) else None
        if isinstance(rollup, dict) and isinstance(rollup.get("state"), str):
            ci_status = rollup["state"]

    return {
        "repository": {"nameWithOwner": repository.get("nameWithOwner")},
        "number": item.get("number"),
        "title": item.get("title"),
        "updatedAt": item.get("updatedAt"),
        "url": item.get("url"),
        "commentsCount": comments_count,
        "labels": labels,
        "isDraft": bool(item.get("isDraft", False)),
        "reviewDecision": item.get("reviewDecision"),
        "reviewRequestCount": review_request_count,
        "unresolvedReviewThreadCount": active_thread_count,
        "reviewThreadAuthorActionCount": author_action_thread_count,
        "reviewThreadReviewerActionCount": reviewer_action_thread_count,
        "reviewThreadsTruncated": (
            isinstance(thread_total, int) and thread_total > len(thread_nodes)
        ),
        "mergeStateStatus": item.get("mergeStateStatus"),
        "ciStatus": ci_status,
        "triageAvailable": True,
    }


def fetch_open_prs_graphql(
    author: str,
    *,
    limit: int,
    token: str,
    request_graphql: Any = graphql_request_json,
) -> list[dict[str, Any]]:
    """Fetch rich PR review and CI signals in batches through GraphQL."""
    results: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(results) < limit:
        variables = {
            "query": f"is:pr is:open author:{author} sort:updated-desc",
            "first": min(GRAPHQL_PAGE_SIZE, limit - len(results)),
            "after": cursor,
        }
        data = request_graphql(TRIAGE_QUERY, variables, token=token)
        search = data.get("search")
        if not isinstance(search, dict):
            raise CLIError("GitHub GraphQL response did not contain search results.")
        nodes = search.get("nodes")
        if not isinstance(nodes, list):
            raise CLIError("GitHub GraphQL search did not contain a pull request list.")
        results.extend(
            normalize_graphql_pr(node)
            for node in nodes
            if isinstance(node, dict) and isinstance(node.get("repository"), dict)
        )

        page_info = search.get("pageInfo")
        if not isinstance(page_info, dict):
            raise CLIError("GitHub GraphQL search did not contain pagination data.")
        if not page_info.get("hasNextPage") or not nodes:
            break
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise CLIError("GitHub GraphQL search returned an invalid pagination cursor.")
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
    normalized = {
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
    if pr.get("triageAvailable"):
        normalized.update(
            {
                "reviewDecision": pr.get("reviewDecision"),
                "reviewRequestCount": pr.get("reviewRequestCount", 0),
                "unresolvedReviewThreadCount": pr.get("unresolvedReviewThreadCount", 0),
                "reviewThreadAuthorActionCount": pr.get(
                    "reviewThreadAuthorActionCount", 0
                ),
                "reviewThreadReviewerActionCount": pr.get(
                    "reviewThreadReviewerActionCount", 0
                ),
                "reviewThreadsTruncated": bool(pr.get("reviewThreadsTruncated", False)),
                "mergeStateStatus": pr.get("mergeStateStatus"),
                "ciStatus": pr.get("ciStatus"),
            }
        )
    return normalized


def classify_attention(
    pr: dict[str, Any],
    *,
    stale_after_days: int,
) -> tuple[str, str]:
    """Classify a rich PR record by the next observable workflow step."""
    if pr["isDraft"]:
        return "draft", "Draft PR; continue author work before requesting review."

    review = pr.get("reviewDecision")
    ci_status = pr.get("ciStatus")
    merge_status = pr.get("mergeStateStatus")
    review_requests = pr.get("reviewRequestCount", 0)
    unresolved_threads = pr.get("unresolvedReviewThreadCount", 0)
    author_action_threads = pr.get("reviewThreadAuthorActionCount", 0)
    reviewer_action_threads = pr.get("reviewThreadReviewerActionCount", 0)

    if review == "CHANGES_REQUESTED":
        return "author-action", "A reviewer requested changes."
    if pr.get("reviewThreadsTruncated"):
        return "author-action", "Review thread data exceeds the query window; inspect the PR."
    if isinstance(author_action_threads, int) and author_action_threads > 0:
        return (
            "author-action",
            f"{author_action_threads} unresolved review thread(s) await an author reply.",
        )
    classified_threads = (
        author_action_threads + reviewer_action_threads
        if isinstance(author_action_threads, int) and isinstance(reviewer_action_threads, int)
        else 0
    )
    if (
        isinstance(unresolved_threads, int)
        and unresolved_threads > classified_threads
    ):
        return "author-action", "An unresolved review thread needs inspection."
    if ci_status in {"ERROR", "FAILURE"}:
        return "author-action", f"CI status is {signal_text(ci_status)}."
    if merge_status == "DIRTY":
        return "author-action", "The PR has merge conflicts."
    if merge_status == "BEHIND":
        return "author-action", "The branch is behind its base branch."
    if ci_status in {"EXPECTED", "PENDING"}:
        return "waiting-ci", f"CI status is {signal_text(ci_status)}."
    if isinstance(reviewer_action_threads, int) and reviewer_action_threads > 0:
        return (
            "waiting-review",
            f"The author replied in {reviewer_action_threads} unresolved review thread(s).",
        )
    if (
        review == "APPROVED"
        and ci_status in {None, "SUCCESS"}
        and merge_status in {"CLEAN", "HAS_HOOKS"}
        and unresolved_threads == 0
    ):
        return "ready-for-maintainer", "Reviews are approved and no failing check is visible."
    if review == "REVIEW_REQUIRED" or (
        isinstance(review_requests, int) and review_requests > 0
    ):
        return "waiting-review", "A review is required or currently requested."
    if pr["ageDays"] >= stale_after_days:
        return "follow-up-candidate", f"No GitHub activity for {pr['ageDays']} days."
    return "monitoring", "No immediate action signal was detected."


def signal_text(value: str) -> str:
    return value.lower().replace("_", " ")


def build_report_data(
    prs: Sequence[dict[str, Any]],
    *,
    author: str,
    stale_after_days: int,
    now: datetime,
    triage: bool = False,
) -> dict[str, Any]:
    """Build a stable report model shared by Markdown and JSON output."""
    normalized = [report_pr(pr, now) for pr in prs]
    ordered_prs = sorted(normalized, key=lambda pr: pr["updatedAt"], reverse=True)
    data = {
        "author": author,
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "staleAfterDays": stale_after_days,
        "pullRequestsInReport": len(ordered_prs),
        "mode": "triage" if triage else "activity",
    }
    if triage:
        triage_counts = {key: 0 for key, _heading, _guidance in TRIAGE_SECTIONS}
        for pr in ordered_prs:
            category, reason = classify_attention(
                pr,
                stale_after_days=stale_after_days,
            )
            pr["attentionCategory"] = category
            pr["attentionReason"] = reason
            triage_counts[category] += 1
        data["pullRequests"] = ordered_prs
        data["triageCounts"] = triage_counts
    else:
        recent, stale = [], []
        for pr in ordered_prs:
            (stale if pr["ageDays"] >= stale_after_days else recent).append(pr)
        data["recent"] = recent
        data["stale"] = stale
    return data


def render_markdown(data: dict[str, Any]) -> str:
    """Render a readable Markdown report from normalized report data."""
    author = data["author"]
    stale_after_days = data["staleAfterDays"]
    generated = parse_timestamp(data["generatedAt"])

    triage_mode = data.get("mode") == "triage"
    lines = [
        "# Open Pull Request Triage" if triage_mode else "# Open Pull Request Follow-up",
        "",
        f"Account: [`{author}`](https://github.com/{author})",
        f"Generated: {generated.date().isoformat()} UTC",
        f"PRs in report: {data['pullRequestsInReport']}",
        "",
    ]
    if triage_mode:
        for key, heading, guidance in TRIAGE_SECTIONS:
            items = [
                pr
                for pr in data["pullRequests"]
                if pr["attentionCategory"] == key
            ]
            if not items:
                continue
            lines.extend([f"## {heading} ({len(items)})", "", guidance, ""])
            for pr in items:
                lines.append(render_pr_line(pr, include_triage=True))
            lines.append("")
        if not data["pullRequestsInReport"]:
            lines.extend(["_No open pull requests found._", ""])
        return "\n".join(lines)

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
            lines.append(render_pr_line(pr))
        lines.append("")
    return "\n".join(lines)


def render_pr_line(pr: dict[str, Any], *, include_triage: bool = False) -> str:
    draft = " (draft)" if pr.get("isDraft") else ""
    comments = pr.get("commentsCount", 0)
    link_text = markdown_link_text(
        f"{pr['repository']}#{pr['number']}: {pr['title']}"
    )
    line = (
        f"- [{link_text}]({pr['url']}){draft} - "
        f"last activity {pr['ageDays']}d ago; {comments} comment(s)"
    )
    if include_triage:
        signals = [
            f"review: {signal_text(pr['reviewDecision'])}"
            if isinstance(pr.get("reviewDecision"), str)
            else None,
            f"CI: {signal_text(pr['ciStatus'])}"
            if isinstance(pr.get("ciStatus"), str)
            else None,
            f"merge: {signal_text(pr['mergeStateStatus'])}"
            if isinstance(pr.get("mergeStateStatus"), str)
            else None,
            f"review threads: {pr['unresolvedReviewThreadCount']} unresolved"
            if isinstance(pr.get("unresolvedReviewThreadCount"), int)
            and pr["unresolvedReviewThreadCount"] > 0
            else None,
        ]
        signal_summary = "; ".join(signal for signal in signals if signal)
        line += f". {pr['attentionReason']}"
        if signal_summary:
            line += f" Signals: {signal_summary}."
    return line


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
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--author", help="GitHub login to report on.")
    parser.add_argument("--stale-after-days", type=int, default=14, help="Days without activity before a PR is grouped as stale (default: 14).")
    parser.add_argument("--json-file", type=Path, help="Read previously saved `gh search prs` JSON instead of calling GitHub.")
    parser.add_argument("--source", choices=("api", "gh"), default="api", help="GitHub data source (default: api).")
    parser.add_argument("--triage", action="store_true", help="Include review, CI, and next-action signals (requires a token).")
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

        token = environment_token()
        if args.triage:
            if args.json_file:
                raise CLIError("--triage cannot be combined with --json-file.")
            if args.source != "api":
                raise CLIError("--triage cannot be combined with --source gh.")
            if not token:
                raise CLIError("--triage requires GH_TOKEN or GITHUB_TOKEN.")
            author = validate_author(args.author or authenticated_login_api(token))
            prs = fetch_open_prs_graphql(author, limit=args.limit, token=token)
        elif args.json_file:
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
            triage=args.triage,
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
