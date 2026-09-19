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
    write_report_file,
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
    discussion_needs_inspection: bool = False,
    discussion_comments_truncated: bool = False,
    discussion_history_incomplete: bool = False,
    latest_discussion_comment_author: str | None = None,
    latest_discussion_comment_at: str | None = None,
    failed_checks: list[dict] | None = None,
    check_contexts_truncated: bool = False,
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
            "discussionNeedsInspection": discussion_needs_inspection,
            "discussionCommentsTruncated": discussion_comments_truncated,
            "discussionHistoryIncomplete": discussion_history_incomplete,
            "latestDiscussionCommentAuthor": latest_discussion_comment_author,
            "latestDiscussionCommentAt": latest_discussion_comment_at,
            "mergeStateStatus": merge,
            "ciStatus": ci,
            "failedChecks": failed_checks or [],
            "checkContextsTruncated": check_contexts_truncated,
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
                                "statusCheckRollup": {
                                    "state": "FAILURE",
                                    "contexts": {
                                        "totalCount": 4,
                                        "nodes": [
                                            {
                                                "__typename": "CheckRun",
                                                "name": "unit-tests",
                                                "status": "COMPLETED",
                                                "conclusion": "FAILURE",
                                                "detailsUrl": "https://github.com/example/project/actions/runs/1",
                                            },
                                            {
                                                "__typename": "CheckRun",
                                                "name": "lint",
                                                "status": "COMPLETED",
                                                "conclusion": "SUCCESS",
                                                "detailsUrl": "https://github.com/example/project/actions/runs/2",
                                            },
                                            {
                                                "__typename": "StatusContext",
                                                "context": "external/build",
                                                "state": "ERROR",
                                                "targetUrl": "https://ci.example.test/build/42",
                                            },
                                        ],
                                    },
                                },
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
        self.assertEqual(
            normalized["failedChecks"],
            [
                {
                    "name": "unit-tests",
                    "result": "FAILURE",
                    "url": "https://github.com/example/project/actions/runs/1",
                },
                {
                    "name": "external/build",
                    "result": "ERROR",
                    "url": "https://ci.example.test/build/42",
                },
            ],
        )
        self.assertTrue(normalized["checkContextsTruncated"])
        self.assertEqual(normalized["unresolvedReviewThreadCount"], 3)
        self.assertEqual(normalized["reviewThreadAuthorActionCount"], 1)
        self.assertEqual(normalized["reviewThreadReviewerActionCount"], 1)
        self.assertTrue(normalized["reviewThreadsTruncated"])
        self.assertTrue(normalized["triageAvailable"])

    def test_normalize_graphql_pr_flags_maintainer_comment_after_head_commit(self) -> None:
        normalized = normalize_graphql_pr(
            {
                "repository": {"nameWithOwner": "example/project"},
                "number": 42,
                "title": "Improve API support",
                "updatedAt": "2026-07-29T12:00:00Z",
                "url": "https://github.com/example/project/pull/42",
                "author": {"login": "octocat"},
                "comments": {
                    "totalCount": 2,
                    "nodes": [
                        {
                            "author": {"login": "maintainer", "__typename": "User"},
                            "authorAssociation": "MEMBER",
                            "createdAt": "2026-07-29T11:00:00Z",
                        },
                        {
                            "author": {"login": "review-bot", "__typename": "Bot"},
                            "createdAt": "2026-07-29T11:30:00Z",
                        },
                    ],
                },
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "committedDate": "2026-07-29T10:00:00Z",
                                "statusCheckRollup": {"state": "SUCCESS"},
                            }
                        }
                    ]
                },
            }
        )

        self.assertTrue(normalized["discussionNeedsInspection"])
        self.assertEqual(normalized["latestDiscussionCommentAuthor"], "maintainer")
        self.assertEqual(normalized["latestDiscussionCommentAt"], "2026-07-29T11:00:00Z")

    def test_normalize_graphql_pr_keeps_discussion_separate_from_review_threads(self) -> None:
        normalized = normalize_graphql_pr(
            {
                "repository": {"nameWithOwner": "example/project"},
                "number": 42,
                "title": "Improve API support",
                "updatedAt": "2026-07-29T12:00:00Z",
                "url": "https://github.com/example/project/pull/42",
                "author": {"login": "octocat"},
                "comments": {
                    "totalCount": 1,
                    "nodes": [
                        {
                            "author": {"login": "maintainer", "__typename": "User"},
                            "authorAssociation": "MEMBER",
                            "createdAt": "2026-07-29T11:00:00Z",
                        }
                    ],
                },
                "reviewThreads": {
                    "totalCount": 1,
                    "nodes": [
                        {
                            "isResolved": False,
                            "isOutdated": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "author": {
                                            "login": "octocat",
                                            "__typename": "User",
                                        }
                                    }
                                ]
                            },
                        }
                    ],
                },
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "committedDate": "2026-07-29T10:00:00Z",
                                "statusCheckRollup": {"state": "SUCCESS"},
                            }
                        }
                    ]
                },
            }
        )

        self.assertEqual(normalized["reviewThreadReviewerActionCount"], 1)
        self.assertTrue(normalized["discussionNeedsInspection"])
        self.assertEqual(normalized["latestDiscussionCommentAuthor"], "maintainer")

    def test_normalize_graphql_pr_does_not_invent_incomplete_discussion_from_thread(self) -> None:
        normalized = normalize_graphql_pr(
            {
                "repository": {"nameWithOwner": "example/project"},
                "number": 42,
                "title": "Improve API support",
                "updatedAt": "2026-07-29T12:00:00Z",
                "url": "https://github.com/example/project/pull/42",
                "author": {"login": "octocat"},
                "comments": {
                    "totalCount": 2,
                    "nodes": [
                        {
                            "author": {"login": "octocat", "__typename": "User"},
                            "authorAssociation": "NONE",
                            "createdAt": "2026-07-29T11:30:00Z",
                        }
                    ],
                },
                "reviewThreads": {
                    "totalCount": 1,
                    "nodes": [
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
                        }
                    ],
                },
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "committedDate": "2026-07-29T10:00:00Z",
                                "statusCheckRollup": {"state": "SUCCESS"},
                            }
                        }
                    ]
                },
            }
        )

        self.assertTrue(normalized["discussionCommentsTruncated"])
        self.assertFalse(normalized["discussionHistoryIncomplete"])
        self.assertEqual(normalized["latestDiscussionCommentAuthor"], "octocat")

    def test_normalize_graphql_pr_clears_maintainer_comment_after_author_response(self) -> None:
        for comments, committed_at in (
            (
                [
                    {
                        "author": {"login": "maintainer", "__typename": "User"},
                        "authorAssociation": "COLLABORATOR",
                        "createdAt": "2026-07-29T11:00:00Z",
                    },
                    {
                        "author": {"login": "OctoCat", "__typename": "User"},
                        "createdAt": "2026-07-29T11:30:00Z",
                    },
                ],
                "2026-07-29T10:00:00Z",
            ),
            (
                [
                    {
                        "author": {"login": "maintainer", "__typename": "User"},
                        "authorAssociation": "OWNER",
                        "createdAt": "2026-07-29T11:00:00Z",
                    }
                ],
                "2026-07-29T12:00:00Z",
            ),
        ):
            with self.subTest(comments=comments, committed_at=committed_at):
                normalized = normalize_graphql_pr(
                    {
                        "repository": {"nameWithOwner": "example/project"},
                        "number": 42,
                        "title": "Improve API support",
                        "updatedAt": "2026-07-29T12:00:00Z",
                        "url": "https://github.com/example/project/pull/42",
                        "author": {"login": "octocat"},
                        "comments": {"totalCount": len(comments), "nodes": comments},
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "committedDate": committed_at,
                                        "statusCheckRollup": {"state": "SUCCESS"},
                                    }
                                }
                            ]
                        },
                    }
                )

                self.assertFalse(normalized["discussionNeedsInspection"])

    def test_normalize_graphql_pr_ignores_untrusted_automation_comment(self) -> None:
        normalized = normalize_graphql_pr(
            {
                "repository": {"nameWithOwner": "example/project"},
                "number": 42,
                "title": "Improve API support",
                "updatedAt": "2026-07-29T12:00:00Z",
                "url": "https://github.com/example/project/pull/42",
                "author": {"login": "octocat"},
                "comments": {
                    "totalCount": 1,
                    "nodes": [
                        {
                            "author": {"login": "CLAassistant", "__typename": "User"},
                            "authorAssociation": "NONE",
                            "createdAt": "2026-07-29T11:00:00Z",
                        }
                    ],
                },
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "committedDate": "2026-07-29T10:00:00Z",
                                "statusCheckRollup": {"state": "SUCCESS"},
                            }
                        }
                    ]
                },
            }
        )

        self.assertFalse(normalized["discussionNeedsInspection"])
        self.assertIsNone(normalized["latestDiscussionCommentAuthor"])

    def test_normalize_graphql_pr_flags_incomplete_discussion_history(self) -> None:
        comments = [
            {
                "author": {"login": f"participant-{index}", "__typename": "User"},
                "authorAssociation": "NONE",
                "createdAt": f"2026-07-29T11:{index:02d}:00Z",
            }
            for index in range(10)
        ]

        normalized = normalize_graphql_pr(
            {
                "repository": {"nameWithOwner": "example/project"},
                "number": 42,
                "title": "Improve API support",
                "updatedAt": "2026-07-29T12:00:00Z",
                "url": "https://github.com/example/project/pull/42",
                "author": {"login": "octocat"},
                "comments": {"totalCount": 11, "nodes": comments},
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "committedDate": "2026-07-29T10:00:00Z",
                                "statusCheckRollup": {"state": "SUCCESS"},
                            }
                        }
                    ]
                },
            }
        )

        self.assertTrue(normalized["discussionCommentsTruncated"])
        self.assertTrue(normalized["discussionHistoryIncomplete"])
        self.assertFalse(normalized["discussionNeedsInspection"])

    def test_normalize_graphql_pr_accepts_truncated_window_with_relevant_comment(self) -> None:
        comments = [
            {
                "author": {"login": f"participant-{index}", "__typename": "User"},
                "authorAssociation": "NONE",
                "createdAt": f"2026-07-29T11:{index:02d}:00Z",
            }
            for index in range(9)
        ]
        comments.append(
            {
                "author": {"login": "OctoCat", "__typename": "User"},
                "authorAssociation": "NONE",
                "createdAt": "2026-07-29T11:30:00Z",
            }
        )

        normalized = normalize_graphql_pr(
            {
                "repository": {"nameWithOwner": "example/project"},
                "number": 42,
                "title": "Improve API support",
                "updatedAt": "2026-07-29T12:00:00Z",
                "url": "https://github.com/example/project/pull/42",
                "author": {"login": "octocat"},
                "comments": {"totalCount": 11, "nodes": comments},
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "committedDate": "2026-07-29T10:00:00Z",
                                "statusCheckRollup": {"state": "SUCCESS"},
                            }
                        }
                    ]
                },
            }
        )

        self.assertTrue(normalized["discussionCommentsTruncated"])
        self.assertFalse(normalized["discussionHistoryIncomplete"])
        self.assertFalse(normalized["discussionNeedsInspection"])

    def test_normalize_graphql_pr_ignores_old_failures_after_rollup_recovers(self) -> None:
        normalized = normalize_graphql_pr(
            {
                "repository": {"nameWithOwner": "example/project"},
                "number": 42,
                "title": "Improve API support",
                "updatedAt": "2026-07-29T12:00:00Z",
                "url": "https://github.com/example/project/pull/42",
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "statusCheckRollup": {
                                    "state": "SUCCESS",
                                    "contexts": {
                                        "totalCount": 1,
                                        "nodes": [
                                            {
                                                "__typename": "CheckRun",
                                                "name": "superseded run",
                                                "status": "COMPLETED",
                                                "conclusion": "FAILURE",
                                                "detailsUrl": "https://github.com/example/project/actions/runs/1",
                                            }
                                        ],
                                    },
                                }
                            }
                        }
                    ]
                },
            }
        )

        self.assertEqual(normalized["ciStatus"], "SUCCESS")
        self.assertEqual(normalized["failedChecks"], [])
        self.assertFalse(normalized["checkContextsTruncated"])

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

    def test_api_fetch_deduplicates_prs_that_move_between_pages(self) -> None:
        requested_pages: list[int] = []

        def request_json(url: str, *, token: str | None) -> dict:
            page = int(parse_qs(urlparse(url).query)["page"][0])
            requested_pages.append(page)
            numbers = list(range(1, 101)) if page == 1 else [100, 101]
            return {
                "total_count": 101,
                "items": [
                    {
                        "repository_url": "https://api.github.com/repos/example/project",
                        "number": number,
                        "title": f"PR {number}",
                        "updated_at": "2026-07-29T12:00:00Z",
                        "html_url": f"https://github.com/example/project/pull/{number}",
                    }
                    for number in numbers
                ],
            }

        prs = fetch_open_prs_api(
            "octocat",
            limit=101,
            request_json=request_json,
        )

        self.assertEqual(requested_pages, [1, 2])
        self.assertEqual([item["number"] for item in prs], list(range(1, 102)))

    def test_api_fetch_rejects_incomplete_search_pages(self) -> None:
        for incomplete_page, empty in ((1, True), (1, False), (2, False)):
            with self.subTest(incomplete_page=incomplete_page, empty=empty):
                requested_pages = []

                def request_json(url: str, *, token: str | None) -> dict:
                    page = int(parse_qs(urlparse(url).query)["page"][0])
                    requested_pages.append(page)
                    return {
                        "total_count": 0 if empty else 2,
                        "incomplete_results": page == incomplete_page,
                        "items": [] if empty else [
                            {
                                "repository_url": "https://api.github.com/repos/example/project",
                                "number": page,
                                "title": f"PR {page}",
                                "updated_at": "2026-07-29T12:00:00Z",
                                "html_url": f"https://github.com/example/project/pull/{page}",
                            }
                        ],
                    }

                with self.assertRaisesRegex(CLIError, "incomplete"):
                    fetch_open_prs_api(
                        "octocat", limit=incomplete_page, request_json=request_json
                    )

                self.assertEqual(requested_pages, list(range(1, incomplete_page + 1)))

    def test_api_fetch_accepts_complete_empty_search(self) -> None:
        def request_json(_url: str, *, token: str | None) -> dict:
            return {"total_count": 0, "incomplete_results": False, "items": []}

        self.assertEqual(
            fetch_open_prs_api("octocat", limit=100, request_json=request_json), []
        )

    def test_main_preserves_report_when_search_is_incomplete(self) -> None:
        def request_json(_url: str, *, token: str | None) -> dict:
            return {"total_count": 0, "incomplete_results": True, "items": []}

        def fetch(author: str, **kwargs) -> list[dict]:
            return fetch_open_prs_api(author, request_json=request_json, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            output.write_text("previous report\n", encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()

            with (
                patch("oss_pr_followup.fetch_open_prs_api", side_effect=fetch),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = main(
                    ["--author", "octocat", "--format", "json", "--output", str(output)]
                )

            self.assertEqual(status, 2)
            self.assertIn("incomplete", stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(output.read_text(encoding="utf-8"), "previous report\n")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_graphql_fetch_uses_cursor_pagination(self) -> None:
        cursors: list[str | None] = []
        page_sizes: list[int] = []

        def request_graphql(_query: str, variables: dict, *, token: str) -> dict:
            self.assertEqual(token, "secret")
            self.assertEqual(variables["checkContextsFirst"], 50)
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

    def test_graphql_fetch_continues_after_duplicate_cursor_page(self) -> None:
        cursors: list[str | None] = []

        def request_graphql(_query: str, variables: dict, *, token: str) -> dict:
            cursors.append(variables["after"])
            pages = (
                (list(range(1, 11)), True, "after-first"),
                ([10], True, "after-duplicate"),
                ([11], False, None),
            )
            numbers, has_next_page, end_cursor = pages[len(cursors) - 1]
            return {
                "search": {
                    "nodes": [
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
                        for number in numbers
                    ],
                    "pageInfo": {
                        "hasNextPage": has_next_page,
                        "endCursor": end_cursor,
                    },
                }
            }

        prs = fetch_open_prs_graphql(
            "octocat",
            limit=11,
            token="secret",
            request_graphql=request_graphql,
        )

        self.assertEqual(cursors, [None, "after-first", "after-duplicate"])
        self.assertEqual([item["number"] for item in prs], list(range(1, 12)))

    def test_graphql_fetch_rejects_repeated_cursor(self) -> None:
        calls = 0

        def request_graphql(_query: str, variables: dict, *, token: str) -> dict:
            nonlocal calls
            calls += 1
            return {
                "search": {
                    "nodes": [
                        {
                            "repository": {"nameWithOwner": "example/project"},
                            "number": 1,
                            "title": "PR 1",
                            "updatedAt": "2026-07-29T12:00:00Z",
                            "url": "https://github.com/example/project/pull/1",
                            "comments": {"totalCount": 0},
                            "labels": {"nodes": []},
                            "reviewRequests": {"totalCount": 0},
                            "commits": {"nodes": []},
                        }
                    ],
                    "pageInfo": {
                        "hasNextPage": True,
                        "endCursor": "unchanged",
                    },
                }
            }

        with self.assertRaisesRegex(CLIError, "repeated.*cursor"):
            fetch_open_prs_graphql(
                "octocat",
                limit=2,
                token="secret",
                request_graphql=request_graphql,
            )

        self.assertEqual(calls, 2)

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
            (
                rich_pr(
                    3,
                    ci="FAILURE",
                    failed_checks=[
                        {"name": "tests", "result": "FAILURE", "url": None}
                    ],
                ),
                "author-action",
            ),
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
            (rich_pr(12, discussion_history_incomplete=True), "author-action"),
            (rich_pr(13, discussion_comments_truncated=True), "monitoring"),
            (
                rich_pr(
                    14,
                    discussion_needs_inspection=True,
                    latest_discussion_comment_author="maintainer",
                    latest_discussion_comment_at="2026-07-29T13:00:00Z",
                ),
                "author-action",
            ),
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

    def test_triage_surfaces_failed_check_evidence(self) -> None:
        failed_checks = [
            {
                "name": "test [windows]",
                "result": "FAILURE",
                "url": "https://github.com/example/project/actions/runs/1",
            },
            {
                "name": "typecheck",
                "result": "TIMED_OUT",
                "url": None,
            },
        ]
        data = build_report_data(
            [
                rich_pr(
                    15,
                    ci="FAILURE",
                    failed_checks=failed_checks,
                    check_contexts_truncated=True,
                )
            ],
            author="octocat",
            stale_after_days=14,
            now=NOW,
            triage=True,
        )

        pull_request = data["pullRequests"][0]
        report = render_markdown(data)

        self.assertEqual(pull_request["failedChecks"], failed_checks)
        self.assertTrue(pull_request["checkContextsTruncated"])
        self.assertIn("1 visible CI check reported failure", pull_request["attentionReason"])
        self.assertIn("test [windows]", pull_request["attentionReason"])
        self.assertIn(r"non-success checks: test \[windows\], typecheck", report)
        self.assertIn("check contexts: truncated", report)

    def test_triage_markdown_neutralizes_check_name_injection(self) -> None:
        injected_name = "tests\n- [forged action](https://example.invalid)<script>"
        data = build_report_data(
            [
                rich_pr(
                    19,
                    ci="FAILURE",
                    failed_checks=[
                        {"name": injected_name, "result": "FAILURE", "url": None}
                    ],
                )
            ],
            author="octocat",
            stale_after_days=14,
            now=NOW,
            triage=True,
        )

        report = render_markdown(data)

        self.assertNotIn("\n- [forged action]", report)
        self.assertNotIn("<script>", report)
        self.assertIn(
            r"tests - \[forged action\](https://example.invalid)&lt;script&gt;",
            report,
        )

    def test_triage_does_not_treat_cancelled_checks_as_author_action(self) -> None:
        data = build_report_data(
            [
                rich_pr(
                    16,
                    ci="FAILURE",
                    failed_checks=[
                        {
                            "name": "openapi-checks",
                            "result": "CANCELLED",
                            "url": "https://github.com/example/project/actions/runs/2",
                        },
                        {
                            "name": "typegen-checks",
                            "result": "TIMED_OUT",
                            "url": "https://github.com/example/project/actions/runs/3",
                        },
                    ],
                )
            ],
            author="octocat",
            stale_after_days=14,
            now=NOW,
            triage=True,
        )

        pull_request = data["pullRequests"][0]
        report = render_markdown(data)

        self.assertEqual(pull_request["attentionCategory"], "ci-investigation")
        self.assertEqual(data["triageCounts"]["author-action"], 0)
        self.assertIn("cancelled", pull_request["attentionReason"].lower())
        self.assertIn("timed out", pull_request["attentionReason"].lower())
        self.assertIn("## CI needs investigation (1)", report)

    def test_triage_investigates_failed_rollup_without_check_evidence(self) -> None:
        data = build_report_data(
            [rich_pr(17, ci="FAILURE")],
            author="octocat",
            stale_after_days=14,
            now=NOW,
            triage=True,
        )

        pull_request = data["pullRequests"][0]

        self.assertEqual(pull_request["attentionCategory"], "ci-investigation")
        self.assertEqual(data["triageCounts"]["author-action"], 0)
        self.assertIn("no concrete failed check", pull_request["attentionReason"].lower())

    def test_merge_conflict_takes_priority_over_inconclusive_ci(self) -> None:
        data = build_report_data(
            [
                rich_pr(
                    18,
                    ci="FAILURE",
                    merge="DIRTY",
                    failed_checks=[
                        {"name": "tests", "result": "CANCELLED", "url": None}
                    ],
                )
            ],
            author="octocat",
            stale_after_days=14,
            now=NOW,
            triage=True,
        )

        pull_request = data["pullRequests"][0]

        self.assertEqual(pull_request["attentionCategory"], "author-action")
        self.assertEqual(pull_request["attentionReason"], "The PR has merge conflicts.")

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

    def test_triage_markdown_surfaces_maintainer_discussion(self) -> None:
        data = build_report_data(
            [
                rich_pr(
                    14,
                    discussion_needs_inspection=True,
                    latest_discussion_comment_author="maintainer",
                    latest_discussion_comment_at="2026-07-29T13:00:00Z",
                )
            ],
            author="octocat",
            stale_after_days=14,
            now=NOW,
            triage=True,
        )

        report = render_markdown(data)

        self.assertIn("latest maintainer discussion comment", report)
        self.assertIn("discussion: inspect @maintainer's latest comment", report)

    def test_triage_surfaces_incomplete_discussion_history(self) -> None:
        data = build_report_data(
            [
                rich_pr(
                    16,
                    discussion_comments_truncated=True,
                    discussion_history_incomplete=True,
                )
            ],
            author="octocat",
            stale_after_days=14,
            now=NOW,
            triage=True,
        )

        pull_request = data["pullRequests"][0]
        report = render_markdown(data)

        self.assertTrue(pull_request["discussionCommentsTruncated"])
        self.assertTrue(pull_request["discussionHistoryIncomplete"])
        self.assertEqual(pull_request["attentionCategory"], "author-action")
        self.assertIn("discussion history exceeds the query window", report)
        self.assertIn("discussion: older comments omitted", report)

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

    def test_report_file_replace_failure_preserves_previous_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            output.write_text("previous report\n", encoding="utf-8")

            with (
                patch("oss_pr_followup.os.replace", side_effect=OSError("replace failed")),
                self.assertRaisesRegex(OSError, "replace failed"),
            ):
                write_report_file(output, "new report")

            self.assertEqual(output.read_text(encoding="utf-8"), "previous report\n")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_report_file_atomically_replaces_previous_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.md"
            output.write_text("previous report\n", encoding="utf-8")

            write_report_file(output, "new report")

            self.assertEqual(output.read_text(encoding="utf-8"), "new report\n")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_report_file_creates_temporary_file_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.md"

            with patch("oss_pr_followup.os.open", wraps=os.open) as secure_open:
                write_report_file(output, "private pull request titles")

            self.assertEqual(secure_open.call_args.args[2], 0o600)

    @unittest.skipIf(os.name == "nt", "Windows does not expose POSIX file modes")
    def test_report_file_preserves_existing_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.md"
            output.write_text("previous report\n", encoding="utf-8")
            output.chmod(0o640)

            write_report_file(output, "new report")

            self.assertEqual(output.stat().st_mode & 0o777, 0o640)

    def test_main_preserves_previous_report_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "prs.json"
            payload.write_text("[]", encoding="utf-8")
            output = Path(directory) / "report.md"
            output.write_text("previous report\n", encoding="utf-8")
            stderr = io.StringIO()

            with (
                patch("oss_pr_followup.os.replace", side_effect=OSError("replace failed")),
                redirect_stderr(stderr),
            ):
                status = main(
                    [
                        "--author",
                        "octocat",
                        "--json-file",
                        str(payload),
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(status, 2)
            self.assertEqual(stderr.getvalue(), "error: replace failed\n")
            self.assertEqual(output.read_text(encoding="utf-8"), "previous report\n")
            self.assertEqual(set(Path(directory).iterdir()), {payload, output})

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

    def test_main_can_signal_author_action_after_rendering_report(self) -> None:
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"GH_TOKEN": "secret"}, clear=True),
            patch(
                "oss_pr_followup.fetch_open_prs_graphql",
                return_value=[rich_pr(10, review="CHANGES_REQUESTED")],
            ),
            redirect_stdout(stdout),
        ):
            status = main(
                [
                    "--author",
                    "octocat",
                    "--triage",
                    "--fail-on-author-action",
                ]
            )

        self.assertEqual(status, 1)
        self.assertIn("## Author action needed (1)", stdout.getvalue())

    def test_main_does_not_fail_when_triage_has_no_author_action(self) -> None:
        with (
            patch.dict(os.environ, {"GH_TOKEN": "secret"}, clear=True),
            patch(
                "oss_pr_followup.fetch_open_prs_graphql",
                return_value=[rich_pr(10, review="REVIEW_REQUIRED")],
            ),
            redirect_stdout(io.StringIO()),
        ):
            status = main(
                [
                    "--author",
                    "octocat",
                    "--triage",
                    "--fail-on-author-action",
                ]
            )

        self.assertEqual(status, 0)

    def test_main_does_not_fail_for_cancelled_ci_without_failure_evidence(self) -> None:
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"GH_TOKEN": "secret"}, clear=True),
            patch(
                "oss_pr_followup.fetch_open_prs_graphql",
                return_value=[
                    rich_pr(
                        10,
                        ci="FAILURE",
                        failed_checks=[
                            {
                                "name": "typegen-checks",
                                "result": "CANCELLED",
                                "url": None,
                            }
                        ],
                    )
                ],
            ),
            redirect_stdout(stdout),
        ):
            status = main(
                [
                    "--author",
                    "octocat",
                    "--triage",
                    "--fail-on-author-action",
                ]
            )

        self.assertEqual(status, 0)
        self.assertIn("## CI needs investigation (1)", stdout.getvalue())

    def test_fail_on_author_action_requires_triage_mode(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = main(["--author", "octocat", "--fail-on-author-action"])

        self.assertEqual(status, 2)
        self.assertEqual(
            stderr.getvalue(),
            "error: --fail-on-author-action requires --triage.\n",
        )


if __name__ == "__main__":
    unittest.main()
