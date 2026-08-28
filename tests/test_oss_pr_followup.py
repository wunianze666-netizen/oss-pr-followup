import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from oss_pr_followup import (
    CLIError,
    age_in_days,
    api_request_json,
    build_report_data,
    fetch_open_prs_api,
    fetch_open_prs_graphql,
    graphql_request_json,
    main,
    normalize_api_pr,
    normalize_graphql_pr,
    read_json_request,
    render_markdown,
    render_report,
    validate_author,
)


NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def pr(number: int, updated_at: str, *, draft: bool = False) -> dict:
    return {
        "repository": {"nameWithOwner": "example/project"},
        "number": number,
        "title": f"Improve example {number}",
        "updatedAt": updated_at,
        "url": f"https://github.com/example/project/pull/{number}",
        "commentsCount": 2,
        "labels": [{"name": "documentation"}],
        "isDraft": draft,
    }


def rich_pr(
    number: int,
    *,
    review: str | None = None,
    ci: str | None = None,
    merge: str = "CLEAN",
    review_requests: int = 0,
    unresolved_threads: int = 0,
    author_action_threads: int = 0,
    reviewer_action_threads: int = 0,
    review_threads_truncated: bool = False,
    draft: bool = False,
) -> dict:
    item = pr(number, "2026-07-29T12:00:00Z", draft=draft)
    item.update(
        {
            "reviewDecision": review,
            "reviewRequestCount": review_requests,
            "unresolvedReviewThreadCount": unresolved_threads,
            "reviewThreadAuthorActionCount": author_action_threads,
            "reviewThreadReviewerActionCount": reviewer_action_threads,
            "reviewThreadsTruncated": review_threads_truncated,
            "mergeStateStatus": merge,
            "ciStatus": ci,
            "triageAvailable": True,
        }
    )
    return item


class ReportTests(unittest.TestCase):
    def test_age_in_days_handles_utc_timestamp(self) -> None:
        self.assertEqual(age_in_days("2026-07-16T12:00:00Z", NOW), 13)

    def test_report_groups_recent_and_stale_prs(self) -> None:
        report = render_report(
            [pr(10, "2026-07-29T12:00:00Z"), pr(20, "2026-07-01T12:00:00Z", draft=True)],
            author="octocat",
            stale_after_days=14,
            now=NOW,
        )

        self.assertIn("## Recent activity", report)
        self.assertIn("example/project#10", report)
        self.assertIn("## No activity for 14+ days", report)
        self.assertIn("example/project#20", report)
        self.assertIn("(draft)", report)

    def test_report_escapes_markdown_in_pr_title(self) -> None:
        unsafe_pr = pr(10, "2026-07-29T12:00:00Z")
        unsafe_pr["title"] = "Handle [brackets](safely)"

        report = render_report(
            [unsafe_pr],
            author="octocat",
            stale_after_days=14,
            now=NOW,
        )

        self.assertIn(r"Handle \[brackets\](safely)", report)

    def test_author_validation_rejects_search_syntax(self) -> None:
        with self.assertRaises(CLIError):
            validate_author("octocat is:closed")

    def test_report_data_is_machine_readable(self) -> None:
        data = build_report_data(
            [pr(10, "2026-07-29T12:00:00Z")],
            author="octocat",
            stale_after_days=14,
            now=NOW,
        )

        self.assertEqual(data["pullRequestsInReport"], 1)
        self.assertEqual(data["recent"][0]["repository"], "example/project")
        self.assertEqual(data["recent"][0]["labels"], ["documentation"])
        self.assertEqual(data["recent"][0]["ageDays"], 0)

    def test_normalize_api_pr_maps_search_fields(self) -> None:
        normalized = normalize_api_pr(
            {
                "repository_url": "https://api.github.com/repos/example/project",
                "number": 42,
                "title": "Improve API support",
                "updated_at": "2026-07-29T12:00:00Z",
                "html_url": "https://github.com/example/project/pull/42",
                "comments": 3,
                "draft": True,
                "labels": [{"name": "enhancement"}],
            }
        )

        self.assertEqual(normalized["repository"]["nameWithOwner"], "example/project")
        self.assertEqual(normalized["commentsCount"], 3)
        self.assertEqual(normalized["labels"], ["enhancement"])
        self.assertTrue(normalized["isDraft"])

    def test_normalize_graphql_pr_maps_triage_signals(self) -> None:
        normalized = normalize_graphql_pr(
            {
                "repository": {"nameWithOwner": "example/project"},
                "number": 42,
                "title": "Improve API support",
                "updatedAt": "2026-07-29T12:00:00Z",
                "url": "https://github.com/example/project/pull/42",
                "isDraft": False,
                "author": {"login": "octocat"},
                "comments": {"totalCount": 3},
                "labels": {"nodes": [{"name": "enhancement"}]},
                "reviewDecision": "CHANGES_REQUESTED",
                "reviewRequests": {"totalCount": 1},
                "mergeStateStatus": "BLOCKED",
                "reviewThreads": {
                    "totalCount": 5,
                    "nodes": [
                        {
                            "isResolved": False,
                            "isOutdated": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "author": {
                                            "login": "maintainer",
                                            "__typename": "User",
                                        }
                                    }
                                ]
                            },
                        },
                        {
                            "isResolved": False,
                            "isOutdated": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "author": {
                                            "login": "OctoCat",
                                            "__typename": "User",
                                        }
                                    }
                                ]
                            },
                        },
                        {
                            "isResolved": False,
                            "isOutdated": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "author": {
                                            "login": "review-bot",
                                            "__typename": "Bot",
                                        }
                                    }
                                ]
                            },
                        },
                        {"isResolved": True, "isOutdated": False, "comments": {"nodes": []}},
                    ],
                },
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "statusCheckRollup": {"state": "FAILURE"},
                            }
                        }
                    ]
                },
            }
        )

        self.assertEqual(normalized["repository"]["nameWithOwner"], "example/project")
        self.assertEqual(normalized["reviewDecision"], "CHANGES_REQUESTED")
        self.assertEqual(normalized["reviewRequestCount"], 1)
        self.assertEqual(normalized["ciStatus"], "FAILURE")
        self.assertEqual(normalized["unresolvedReviewThreadCount"], 3)
        self.assertEqual(normalized["reviewThreadAuthorActionCount"], 1)
        self.assertEqual(normalized["reviewThreadReviewerActionCount"], 1)
        self.assertTrue(normalized["reviewThreadsTruncated"])
        self.assertTrue(normalized["triageAvailable"])

    def test_api_fetch_paginates_until_limit(self) -> None:
        requested_pages: list[int] = []
        requested_page_sizes: list[int] = []

        def request_json(url: str, *, token: str | None) -> dict:
            self.assertEqual(token, "secret")
            query = parse_qs(urlparse(url).query)
            page = int(query["page"][0])
            requested_pages.append(page)
            requested_page_sizes.append(int(query["per_page"][0]))
            start = (page - 1) * 100
            count = 100 if page == 1 else 50
            return {
                "total_count": 150,
                "items": [
                    {
                        "repository_url": "https://api.github.com/repos/example/project",
                        "number": number,
                        "title": f"PR {number}",
                        "updated_at": "2026-07-29T12:00:00Z",
                        "html_url": f"https://github.com/example/project/pull/{number}",
                        "comments": 0,
                        "labels": [],
                    }
                    for number in range(start + 1, start + count + 1)
                ],
            }

        prs = fetch_open_prs_api(
            "octocat",
            limit=150,
            token="secret",
            request_json=request_json,
        )

        self.assertEqual(requested_pages, [1, 2])
        self.assertEqual(requested_page_sizes, [100, 100])
        self.assertEqual(len(prs), 150)
        self.assertEqual(prs[-1]["number"], 150)

    def test_graphql_fetch_uses_cursor_pagination(self) -> None:
        cursors: list[str | None] = []
        page_sizes: list[int] = []

        def request_graphql(_query: str, variables: dict, *, token: str) -> dict:
            self.assertEqual(token, "secret")
            cursors.append(variables["after"])
            page_sizes.append(variables["first"])
            start = sum(page_sizes[:-1])
            nodes = [
                {
                    "repository": {"nameWithOwner": "example/project"},
                    "number": number,
                    "title": f"PR {number}",
                    "updatedAt": "2026-07-29T12:00:00Z",
                    "url": f"https://github.com/example/project/pull/{number}",
                    "comments": {"totalCount": 0},
                    "labels": {"nodes": []},
                    "reviewRequests": {"totalCount": 0},
                    "commits": {"nodes": []},
                }
                for number in range(start + 1, start + variables["first"] + 1)
            ]
            return {
                "search": {
                    "nodes": nodes,
                    "pageInfo": {
                        "hasNextPage": len(cursors) == 1,
                        "endCursor": "next-page" if len(cursors) == 1 else None,
                    },
                }
            }

        prs = fetch_open_prs_graphql(
            "octocat",
            limit=11,
            token="secret",
            request_graphql=request_graphql,
        )

        self.assertEqual(cursors, [None, "next-page"])
        self.assertEqual(page_sizes, [10, 1])
        self.assertEqual([item["number"] for item in prs], list(range(1, 12)))

    def test_graphql_request_posts_token_and_variables(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b'{"data":{"viewer":{"login":"octocat"}}}'

        def opener(request, *, timeout: int):
            self.assertEqual(timeout, 20)
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
            body = json.loads(request.data.decode("utf-8"))
            self.assertEqual(body["variables"], {"expected": True})
            return Response()

        data = graphql_request_json(
            "query Example { viewer { login } }",
            {"expected": True},
            token="secret",
            opener=opener,
        )

        self.assertEqual(data["viewer"]["login"], "octocat")

    def test_json_request_retries_transient_server_error(self) -> None:
        attempts = 0
        delays: list[float] = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b'{"ok":true}'

        def opener(_request, *, timeout: int):
            nonlocal attempts
            self.assertEqual(timeout, 20)
            attempts += 1
            if attempts == 1:
                raise HTTPError(
                    "https://api.github.com/graphql",
                    503,
                    "Service Unavailable",
                    {"Retry-After": "0"},
                    io.BytesIO(b'{"message":"temporarily unavailable"}'),
                )
            return Response()

        payload = read_json_request(
            Request("https://api.github.com/graphql"),
            opener=opener,
            sleeper=delays.append,
        )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(attempts, 2)
        self.assertEqual(delays, [0.0])

    def test_json_request_retries_transient_network_failures(self) -> None:
        for transient_error in (
            URLError("temporary name resolution failure"),
            TimeoutError(),
        ):
            with self.subTest(error=type(transient_error).__name__):
                attempts = 0
                delays: list[float] = []

                class Response:
                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        return False

                    def read(self) -> bytes:
                        return b'{"ok":true}'

                def opener(_request, *, timeout: int):
                    nonlocal attempts
                    self.assertEqual(timeout, 20)
                    attempts += 1
                    if attempts == 1:
                        raise transient_error
                    return Response()

                payload = read_json_request(
                    Request("https://api.github.com/graphql"),
                    opener=opener,
                    sleeper=delays.append,
                )

                self.assertEqual(payload, {"ok": True})
                self.assertEqual(attempts, 2)
                self.assertEqual(delays, [1.0])

    def test_json_request_stops_after_network_retry_budget(self) -> None:
        attempts = 0
        delays: list[float] = []

        def opener(_request, *, timeout: int):
            nonlocal attempts
            self.assertEqual(timeout, 20)
            attempts += 1
            raise URLError("network unreachable")

        with self.assertRaisesRegex(CLIError, "network unreachable"):
            read_json_request(
                Request("https://api.github.com/graphql"),
                opener=opener,
                sleeper=delays.append,
            )

        self.assertEqual(attempts, 3)
        self.assertEqual(delays, [1.0, 2.0])

    def test_json_request_stops_after_retry_budget(self) -> None:
        attempts = 0
        delays: list[float] = []

        def opener(_request, *, timeout: int):
            nonlocal attempts
            self.assertEqual(timeout, 20)
            attempts += 1
            raise HTTPError(
                "https://api.github.com/graphql",
                502,
                "Bad Gateway",
                {"Retry-After": "0"},
                io.BytesIO(b'{"message":"upstream unavailable"}'),
            )

        with self.assertRaisesRegex(CLIError, r"\(502\): upstream unavailable"):
            read_json_request(
                Request("https://api.github.com/graphql"),
                opener=opener,
                sleeper=delays.append,
            )

        self.assertEqual(attempts, 3)
        self.assertEqual(delays, [0.0, 0.0])

    def test_json_request_does_not_retry_primary_rate_limit(self) -> None:
        attempts = 0
        delays: list[float] = []

        def opener(_request, *, timeout: int):
            nonlocal attempts
            self.assertEqual(timeout, 20)
            attempts += 1
            raise HTTPError(
                "https://api.github.com/graphql",
                403,
                "Forbidden",
                {"Retry-After": "0", "X-RateLimit-Remaining": "0"},
                io.BytesIO(b'{"message":"API rate limit exceeded"}'),
            )

        with self.assertRaisesRegex(CLIError, "rate limit reached"):
            read_json_request(
                Request("https://api.github.com/graphql"),
                opener=opener,
                sleeper=delays.append,
            )

        self.assertEqual(attempts, 1)
        self.assertEqual(delays, [])

    def test_json_request_does_not_wait_past_delay_cap(self) -> None:
        attempts = 0
        delays: list[float] = []

        def opener(_request, *, timeout: int):
            nonlocal attempts
            self.assertEqual(timeout, 20)
            attempts += 1
            raise HTTPError(
                "https://api.github.com/graphql",
                503,
                "Service Unavailable",
                {"Retry-After": "60"},
                io.BytesIO(b'{"message":"maintenance window"}'),
            )

        with self.assertRaisesRegex(CLIError, r"\(503\): maintenance window"):
            read_json_request(
                Request("https://api.github.com/graphql"),
                opener=opener,
                sleeper=delays.append,
            )

        self.assertEqual(attempts, 1)
        self.assertEqual(delays, [])

    def test_triage_classification_prioritizes_actionable_signals(self) -> None:
        cases = (
            (rich_pr(1, draft=True), "draft"),
            (rich_pr(2, review="CHANGES_REQUESTED"), "author-action"),
            (rich_pr(3, ci="FAILURE"), "author-action"),
            (rich_pr(4, merge="DIRTY"), "author-action"),
            (
                rich_pr(8, unresolved_threads=1, author_action_threads=1),
                "author-action",
            ),
            (
                rich_pr(9, unresolved_threads=1, reviewer_action_threads=1),
                "waiting-review",
            ),
            (rich_pr(10, unresolved_threads=1), "author-action"),
            (rich_pr(11, review_threads_truncated=True), "author-action"),
            (rich_pr(5, ci="PENDING"), "waiting-ci"),
            (
                rich_pr(6, review="APPROVED", ci="SUCCESS", merge="CLEAN"),
                "ready-for-maintainer",
            ),
            (rich_pr(7, review="REVIEW_REQUIRED"), "waiting-review"),
        )

        for item, expected in cases:
            normalized = build_report_data(
                [item],
                author="octocat",
                stale_after_days=14,
                now=NOW,
                triage=True,
            )
            with self.subTest(expected=expected):
                self.assertEqual(
                    normalized["pullRequests"][0]["attentionCategory"],
                    expected,
                )

    def test_triage_markdown_explains_attention_reason(self) -> None:
        data = build_report_data(
            [rich_pr(10, review="CHANGES_REQUESTED")],
            author="octocat",
            stale_after_days=14,
            now=NOW,
            triage=True,
        )

        report = render_markdown(data)
        self.assertIn("# Open Pull Request Triage", report)
        self.assertIn("## Author action needed (1)", report)
        self.assertIn("A reviewer requested changes.", report)
        self.assertIn("review: changes requested", report)
        self.assertEqual(data["triageCounts"]["author-action"], 1)
        self.assertNotIn("recent", data)

    def test_behind_branch_does_not_invent_author_action(self) -> None:
        data = build_report_data(
            [
                rich_pr(
                    12,
                    review="REVIEW_REQUIRED",
                    ci="SUCCESS",
                    merge="BEHIND",
                ),
                rich_pr(
                    13,
                    review="APPROVED",
                    ci="SUCCESS",
                    merge="BEHIND",
                ),
            ],
            author="octocat",
            stale_after_days=14,
            now=NOW,
            triage=True,
        )

        waiting_review, monitoring = data["pullRequests"]
        self.assertEqual(waiting_review["attentionCategory"], "waiting-review")
        self.assertEqual(monitoring["attentionCategory"], "monitoring")
        self.assertIn("repository policy", monitoring["attentionReason"])

    def test_triage_markdown_surfaces_unresolved_review_threads(self) -> None:
        data = build_report_data(
            [rich_pr(10, unresolved_threads=2, author_action_threads=2)],
            author="octocat",
            stale_after_days=14,
            now=NOW,
            triage=True,
        )

        report = render_markdown(data)

        self.assertIn("2 unresolved review thread(s) await an author reply.", report)
        self.assertIn("review threads: 2 unresolved", report)

    def test_api_rate_limit_error_does_not_expose_token(self) -> None:
        def rate_limited(*_args, **_kwargs):
            raise HTTPError(
                "https://api.github.com/search/issues",
                403,
                "Forbidden",
                {"X-RateLimit-Remaining": "0"},
                io.BytesIO(b'{"message":"API rate limit exceeded"}'),
            )

        with self.assertRaisesRegex(CLIError, "GITHUB_TOKEN") as error:
            api_request_json(
                "https://api.github.com/search/issues",
                token="sensitive-value",
                opener=rate_limited,
            )

        self.assertNotIn("sensitive-value", str(error.exception))

    def test_main_reads_offline_json_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "prs.json"
            payload.write_bytes(b"\xef\xbb\xbf" + json.dumps([pr(10, "2026-07-29T12:00:00Z")]).encode())
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["--author", "octocat", "--json-file", str(payload)]), 0)

        self.assertIn("example/project#10", stdout.getvalue())

    def test_main_can_write_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "prs.json"
            payload.write_text(json.dumps([pr(10, "2026-07-29T12:00:00Z")]), encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status = main(
                    [
                        "--author",
                        "octocat",
                        "--json-file",
                        str(payload),
                        "--format",
                        "json",
                    ]
                )

        self.assertEqual(status, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["author"], "octocat")
        self.assertEqual(report["pullRequestsInReport"], 1)

    def test_main_reports_missing_author_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "prs.json"
            payload.write_text("[]", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                status = main(["--json-file", str(payload)])

        self.assertEqual(status, 2)
        self.assertEqual(stderr.getvalue(), "error: --author is required with --json-file.\n")

    def test_main_requires_token_for_triage(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
            status = main(["--author", "octocat", "--triage"])

        self.assertEqual(status, 2)
        self.assertEqual(
            stderr.getvalue(),
            "error: --triage requires GH_TOKEN or GITHUB_TOKEN.\n",
        )


if __name__ == "__main__":
    unittest.main()
