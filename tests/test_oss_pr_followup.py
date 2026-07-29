import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from oss_pr_followup import age_in_days, main, render_report


NOW = datetime(2026, 7, 30, tzinfo=UTC)


def pr(number: int, updated_at: str, *, draft: bool = False) -> dict:
    return {
        "repository": {"nameWithOwner": "example/project"},
        "number": number,
        "title": f"Improve example {number}",
        "updatedAt": updated_at,
        "url": f"https://github.com/example/project/pull/{number}",
        "commentsCount": 2,
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

    def test_main_reads_offline_json_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "prs.json"
            payload.write_bytes(b"\xef\xbb\xbf" + json.dumps([pr(10, "2026-07-29T12:00:00Z")]).encode())
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["--author", "octocat", "--json-file", str(payload)]), 0)

        self.assertIn("example/project#10", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
