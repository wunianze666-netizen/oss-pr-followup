import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from oss_pr_followup import (
    CLIError,
    age_in_days,
    api_request_json,
    build_report_data,
    fetch_open_prs_api,
    main,
    normalize_api_pr,
    render_report,
    validate_author,
)


NOW = datetime(2026, 7, 30, tzinfo=UTC)


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


if __name__ == "__main__":
    unittest.main()
