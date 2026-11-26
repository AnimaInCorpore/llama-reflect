import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from refl.githistory import History, is_repository


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          encoding="utf-8").stdout.strip()


def commit(repo, path, text, message):
    (repo / path).write_text(text, encoding="utf-8")
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "--short", "HEAD")


def repository(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    return path


@unittest.skipUnless(shutil.which("git"), "needs git")
class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.repo = repository(base / "ste")
        commit(self.repo, "gen.py", "GUARD = 'all'\n", "Start the translator")
        self.guards = commit(self.repo, "gen.py", "GUARD = 'coverage'\n",
                             "Black Tiger: the window guards only at the coverage's sites; 23.8 -> 26.3 frames/s")
        # A later commit on another branch, as the answer of a history benchmark task would be.
        git(self.repo, "checkout", "-q", "-b", "later")
        self.hidden = commit(self.repo, "gen.py", "GUARD = 'none'\n", "Window guards removed entirely")
        git(self.repo, "checkout", "-q", "-")
        self.sibling = repository(base / "f030")
        commit(self.sibling, "port.s", "move.l d0,d1\n", "Commando: the sprite list copied by the Blitter")
        self.history = History(self.repo, self.sibling)

    def tearDown(self):
        self.directory.cleanup()

    def run_tool(self, name, **arguments):
        return self.history.execute({"name": name, "arguments": json.dumps(arguments)})

    def test_messages_are_searched_in_the_history_of_head_only(self):
        output = self.run_tool("search_history", pattern="window guard")["output"]
        self.assertIn(f"{self.guards} ", output)
        self.assertIn("23.8 -> 26.3 frames/s", output)
        self.assertNotIn("removed entirely", output)
        self.assertEqual(self.run_tool("search_history", pattern="no such thing")["output"], "No commits match.")

    def test_changed_lines_and_the_sibling_are_searched(self):
        self.assertIn(self.guards, self.run_tool("search_history", pattern="coverage", **{"in": "changes"})["output"])
        self.assertIn("Commando", self.run_tool("search_history", pattern="sprite", repo="sibling")["output"])
        self.assertIn("No sibling repository", History(self.repo).execute(
            {"name": "search_history", "arguments": json.dumps({"pattern": "x", "repo": "sibling"})})["error"])

    def test_a_commit_shows_its_message_and_diff(self):
        output = self.run_tool("show_commit", commit=self.guards)["output"]
        self.assertIn("the window guards only at the coverage's sites", output)
        self.assertIn("+GUARD = 'coverage'", output)

    def test_commits_outside_the_history_of_head_are_refused(self):
        self.assertIn("is not in the history of HEAD", self.run_tool("show_commit", commit=self.hidden)["error"])
        self.assertIn("hexadecimal", self.run_tool("show_commit", commit="--all")["error"])

    def test_a_repository_needs_a_commit(self):
        self.assertTrue(is_repository(self.repo))
        self.assertFalse(is_repository(Path(self.directory.name)))


if __name__ == "__main__":
    unittest.main()
