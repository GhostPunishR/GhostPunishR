from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

from scripts import update_profile_graphs as graphs


ROOT = Path(__file__).resolve().parents[1]
VALID_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 340 200">'
             b'<text>Total Commits:</text></svg>')


class RefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "stats.svg"
        self.path.write_bytes(VALID_SVG)
        self.output = StringIO()
        redirect = redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        sleep = patch.object(graphs.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_valid_update_replaces_snapshot(self):
        updated = VALID_SVG.replace(b"</text>", b" 42</text>")
        self.assertTrue(graphs.refresh(self.path, lambda: updated, "Total Commits:"))
        self.assertEqual(self.path.read_bytes(), updated)
        self.assertFalse(self.path.with_suffix(".svg.tmp").exists())

    def test_provider_failures_preserve_snapshot(self):
        errors = [HTTPError("https://example.test", 402, "Disabled", {}, None),
                  TimeoutError("Timed out")]
        for error in errors:
            with self.subTest(error=error), patch.object(graphs, "fetch_summary", side_effect=error) as fetch:
                self.assertTrue(graphs.refresh(self.path, fetch, "Total Commits:"))
                self.assertEqual(self.path.read_bytes(), VALID_SVG)
                self.assertEqual(fetch.call_count, 3)
        self.assertIn("::warning", self.output.getvalue())

    def test_html_error_svg_and_truncated_response_preserve_snapshot(self):
        invalid = [b'<html>Deployment disabled</html>', b'<svg',
                   VALID_SVG.replace(b'Total Commits:', b'API rate limit exceeded'),
                   VALID_SVG.replace(b'0 0 340 200', b'0 0 0 0')]
        for content in invalid:
            with self.subTest(content=content):
                self.assertTrue(graphs.refresh(self.path, lambda: content, "Total Commits:"))
                self.assertEqual(self.path.read_bytes(), VALID_SVG)

    def test_retry_recovers(self):
        with patch.object(graphs, "fetch_summary", side_effect=[TimeoutError(), VALID_SVG]) as fetch:
            self.assertTrue(graphs.refresh(self.path, fetch, "Total Commits:"))
            self.assertEqual(fetch.call_count, 2)

    def test_missing_or_corrupt_fallback_reports_failure(self):
        for existing in (None, b'<html>not an image</html>'):
            if existing is None:
                self.path.unlink()
            else:
                self.path.write_bytes(existing)
            self.assertFalse(graphs.refresh(self.path, lambda: b'bad response', "Total Commits:"))
        self.assertIn("::error", self.output.getvalue())


class ContributionTests(unittest.TestCase):
    NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)

    def days(self, count=0):
        return [((self.NOW - timedelta(days=30 - i)).date().isoformat(), count)
                for i in range(31)]

    def payload(self, days):
        return json.dumps({"data": {"user": {"contributionsCollection": {
            "contributionCalendar": {"weeks": [{"contributionDays": [
                {"date": date, "contributionCount": count} for date, count in days
            ]}]}
        }}}}).encode()

    def test_fetch_calendar_uses_github_api_and_exact_dates(self):
        days = self.days(4)
        with patch.dict(os.environ, {"GH_TOKEN": "test-only"}), \
                patch.object(graphs, "request_bytes", return_value=self.payload(days)) as request:
            self.assertEqual(graphs.fetch_contributions("GhostPunishR", self.NOW), days)
        url = request.call_args.args[0]
        body = json.loads(request.call_args.kwargs["data"])
        self.assertEqual(url, "https://api.github.com/graphql")
        self.assertEqual(body["variables"]["from"], "2026-09-17T00:00:00+00:00")
        self.assertEqual(body["variables"]["login"], "GhostPunishR")

    def test_missing_dates_negative_counts_and_api_errors_are_rejected(self):
        invalid = [self.payload(self.days()[:-1]), self.payload(self.days(-1)),
                   b'{"errors":[{"message":"Rate limited"}]}', b'{"data":{"user":null}}']
        for response in invalid:
            with self.subTest(response=response), patch.dict(os.environ, {"GH_TOKEN": "test-only"}), \
                    patch.object(graphs, "request_bytes", return_value=response):
                with self.assertRaises(ValueError):
                    graphs.fetch_contributions("GhostPunishR", self.NOW)

    def test_missing_token_does_not_call_api(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(graphs, "request_bytes") as request:
            with self.assertRaises(ValueError):
                graphs.fetch_contributions("GhostPunishR", self.NOW)
            request.assert_not_called()

    def test_empty_and_active_calendars_render_with_correct_counts(self):
        for count in (0, 9):
            content = graphs.render_activity(self.days(count), 'Ghost & <test>')
            graphs.validate_svg(content, "Contribution Graph")
            root = ET.fromstring(content)
            circles = root.findall(".//" + graphs.SVG + "circle")
            self.assertEqual(len(circles), 31)
            self.assertIn(f"{31 * count} contributions", root.find(graphs.SVG + "desc").text)
            self.assertIn("Ghost & <test>", root.find(graphs.SVG + "title").text)
            self.assertEqual(circles[-1].find(graphs.SVG + "title").text,
                             f"2026-10-17: {count} contributions")


class ProfileTests(unittest.TestCase):
    def test_refresh_continues_after_a_card_failure_and_reports_missing_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            def summary(name, username):
                if name == "profile-details":
                    raise TimeoutError("Provider unavailable")
                text = graphs.SUMMARY_CARDS[name].encode()
                return VALID_SVG.replace(b'Total Commits:', text)

            with patch.object(graphs, "fetch_summary", side_effect=summary), \
                    patch.object(graphs, "fetch_contributions", return_value=ContributionTests().days()), \
                    patch.object(graphs.time, "sleep"), redirect_stdout(StringIO()), \
                    patch.object(graphs.sys, "argv", ["update", "--username", "GhostPunishR",
                                                     "--output-dir", directory]):
                self.assertEqual(graphs.main(), 1)
                self.assertTrue((Path(directory) / "activity-graph.svg").exists())
                self.assertTrue((Path(directory) / "summary-stats.svg").exists())
                # An existing valid snapshot makes a provider outage non-destructive.
                fallback = VALID_SVG.replace(b'Total Commits:', b'Contributions on GitHub')
                (Path(directory) / "profile-details.svg").write_bytes(fallback)
                self.assertEqual(graphs.main(), 0)
                self.assertEqual((Path(directory) / "profile-details.svg").read_bytes(), fallback)

    def test_all_nine_statistics_images_are_local_and_valid(self):
        from html.parser import HTMLParser

        class Images(HTMLParser):
            def __init__(self):
                super().__init__()
                self.sources = []

            def handle_starttag(self, tag, attrs):
                if tag == "img":
                    self.sources.append(dict(attrs)["src"])

        images = Images()
        images.feed((ROOT / "README.md").read_text())
        cards = [src for src in images.sources if "shields.io" not in src]
        self.assertEqual(len(cards), 9)
        for src in cards:
            self.assertTrue(src.startswith("./profile/"), src)
            self.assertEqual(ET.parse(ROOT / src).getroot().tag, graphs.SVG + "svg")
        for name, expected in graphs.SUMMARY_CARDS.items():
            graphs.validate_svg((ROOT / "profile" / f"{name}.svg").read_bytes(), expected)


if __name__ == "__main__":
    unittest.main()
