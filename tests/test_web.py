import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from refl import web


class WebTest(unittest.TestCase):
    def test_html_becomes_readable_lines(self):
        title, text = web.html_to_text(
            "<html><head><title>MFP timers</title><style>p{}</style></head><body><nav>Menu</nav>"
            "<h2>Timer B</h2><p>Counts  <b>horizontal</b> blanks.</p><script>evil()</script>"
            "<ul><li>Mode 8: event count</li></ul><pre>move.b #8,$fffa1b\n  rts</pre></body></html>")
        self.assertEqual(title, "MFP timers")
        self.assertEqual(text.splitlines(), ["Menu", "## Timer B", "Counts horizontal blanks.",
                                             "- Mode 8: event count", "move.b #8,$fffa1b", "  rts"])

    def test_only_public_http_addresses_are_fetched(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1:8090/slots", "http://localhost/", "http://10.0.0.1/",
                    "http://[::1]/", "http://user:secret@example.com/", "ftp://example.com/x"):
            with self.subTest(url=url):
                self.assertIn("error", web.fetch_page(tempfile.gettempdir(), url))

    def test_a_page_is_saved_with_its_source_and_shown_with_line_numbers(self):
        with tempfile.TemporaryDirectory() as root:
            url = "https://example.com/docs/timers.html"
            now = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)
            relative, lines = web.save_page(root, url, "MFP timers", "Timer B counts blanks.", now)
            self.assertRegex(relative, r"^\.refl/sources/example\.com/timers-[0-9a-f]{8}\.md$")
            self.assertEqual(lines[:3], [f"Source: {url}", "Fetched: 2026-11-26T10:00Z", "Title: MFP timers"])
            self.assertEqual((Path(root) / ".refl" / "sources" / ".gitignore").read_text(encoding="utf-8"), "*\n")
            output = web.result_for(relative, lines)["output"]
            self.assertIn(f"Cite it as {relative}:<line>.", output)
            self.assertIn("5: Timer B counts blanks.", output)
            self.assertEqual(web.saved_sources(root), [f"{relative} ({url})"])

    def test_a_long_page_points_to_the_rest(self):
        lines = [f"line {number}" for number in range(2000)]
        output = web.result_for(".refl/sources/x/a.md", lines)["output"]
        self.assertIn("are in the saved file. Read them with read_file.", output)


if __name__ == "__main__":
    unittest.main()
