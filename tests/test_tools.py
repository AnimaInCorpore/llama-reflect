import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from refl.tools import WorkspaceTools

_NOTE = "[Line limit reached. Continue from line 81.]"


class ReadFileTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.tools = WorkspaceTools(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def lines(self, count):
        path = self.root / f"lines{count}.txt"
        path.write_text("".join(f"line {number}\n" for number in range(1, count + 1)), encoding="utf-8")
        return path.name

    def test_default_range_says_where_to_continue(self):
        output = self.tools.read_file(self.lines(200))["output"]
        self.assertIn("80: line 80", output)
        self.assertNotIn("81: line 81", output)
        self.assertTrue(output.endswith(_NOTE))

    def test_no_note_when_the_file_ends_at_the_limit(self):
        self.assertNotIn("Line limit", self.tools.read_file(self.lines(80))["output"])

    def test_requested_range_past_the_limit(self):
        self.assertTrue(self.tools.read_file(self.lines(85), end_line=100)["output"].endswith(_NOTE))
        self.assertNotIn("Line limit", self.tools.read_file(self.lines(80), end_line=200)["output"])

    def test_explicit_short_range_has_no_note(self):
        output = self.tools.read_file(self.lines(200), start_line=5, end_line=10)["output"]
        self.assertEqual(output.splitlines()[-1], "10: line 10")


if __name__ == "__main__":
    unittest.main()


class EditFileTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.tools = WorkspaceTools(self.root)
        self.path = self.root / "app.py"
        self.path.write_bytes(b"import os\r\n\r\n\r\ndef a():  \r\n    return 1\r\n\r\n\r\ndef b():\r\n    return 2\r\n")

    def tearDown(self):
        self.directory.cleanup()

    def test_literal_match(self):
        result = self.tools.edit_file("app.py", "return 2", "return 3")
        self.assertEqual(result, {"output": "Edited app.py", "changed": True})
        self.assertTrue(self.path.read_bytes().endswith(b"    return 3\r\n"))

    def test_match_ignoring_blank_lines_and_trailing_spaces(self):
        result = self.tools.edit_file("app.py", "def a():\n    return 1\n\ndef b():", "def a():\n    return 10\n\n\ndef b():")
        self.assertTrue(result["changed"])
        self.assertIn("ignoring blank lines", result["output"])
        self.assertEqual(self.path.read_bytes(),
                         b"import os\r\n\r\n\r\ndef a():\r\n    return 10\r\n\r\n\r\ndef b():\r\n    return 2\r\n")

    def test_indentation_still_has_to_match(self):
        with self.assertRaisesRegex(ValueError, "was not found"):
            self.tools.edit_file("app.py", "def a():\nreturn 1", "def a():\n    return 0")

    def test_tolerant_match_must_be_unique(self):
        self.path.write_text("x = 1\n\ny = 2\nx = 1\ny = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "more than once"):
            self.tools.edit_file("app.py", "x = 1\n\n\ny = 2", "z = 3")

    def test_line_numbers_from_read_file(self):
        with self.assertRaisesRegex(ValueError, "Leave out the line numbers"):
            self.tools.edit_file("app.py", "8: def b():\n9:     return 2", "def b():\n    return 3")


_WHICH = shutil.which
_LONG_LINE = "alpha" + "x" * 295 + " [... omitted end of long line]"


def _ripgrep_lines(output):
    """ripgrep's lines with paths as the fallback prints them: relative to the workspace, with slashes."""
    lines = []
    for line in output.splitlines():
        line = line.rstrip("\r").replace("\\", "/")
        while line.startswith("./"):
            line = line[2:]
        lines.append(line)
    return sorted(lines)


class GrepSearchTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        files = {
            "src/a.py": b"alpha\nbeta alpha\n",
            "src/b.py": b"gamma\n",
            ".hidden/h.txt": b"alpha hidden\n",
            "bin.dat": b"alpha\0binary\n",
            "crlf.txt": b"crlf alpha\r\nnext\r\n",
            "long.txt": b"alpha" + b"x" * 400 + b"\n",
            "build/out.txt": b"alpha built\n",
            ".gitignore": b"build/\n",
        }
        for name, data in files.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_bytes(data)
        self.tools = WorkspaceTools(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def search(self, path, pattern):
        """Run grep_search as it runs where ripgrep is not installed."""
        with mock.patch("refl.tools.shutil.which", lambda name: None if name == "rg" else _WHICH(name)):
            return self.tools.grep_search(path, pattern)

    def test_a_directory_search_skips_hidden_and_binary_files(self):
        self.assertEqual(self.search(".", "alpha")["output"].splitlines(), [
            "build/out.txt:1:alpha built",
            "crlf.txt:1:crlf alpha",
            f"long.txt:1:{_LONG_LINE}",
            "src/a.py:1:alpha",
            "src/a.py:2:beta alpha",
        ])

    def test_a_file_search_leaves_out_the_file_name(self):
        self.assertEqual(self.search("src/a.py", "alpha"), {"output": "1:alpha\n2:beta alpha"})

    def test_a_binary_file_is_reported_only_as_the_target(self):
        self.assertEqual(self.search("bin.dat", "alpha"),
                         {"output": 'binary file matches (found "\\0" byte around offset 5)'})

    def test_no_match_and_an_invalid_pattern(self):
        self.assertEqual(self.search("src", "Alpha"), {"output": "No matches."})
        self.assertIn("regex parse error", self.search(".", "a(b")["error"])

    def test_more_than_thirty_matches_are_cut(self):
        (self.root / "many.txt").write_bytes(b"hit\n" * 40)
        lines = self.search("many.txt", "hit")["output"].splitlines()
        self.assertEqual((len(lines), lines[-1]), (31, "[More matches omitted. Narrow the search to continue.]"))

    @unittest.skipUnless(shutil.which("git"), "needs Git")
    def test_gitignore_applies_in_a_git_repository(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        lines = self.search(".", "alpha")["output"].splitlines()
        self.assertEqual(lines, ["crlf.txt:1:crlf alpha", f"long.txt:1:{_LONG_LINE}", "src/a.py:1:alpha",
                                 "src/a.py:2:beta alpha"])
        self.assertEqual(self.search("src", "gamma"), {"output": "src/b.py:1:gamma"})

    @unittest.skipUnless(shutil.which("rg"), "needs ripgrep")
    def test_the_fallback_finds_what_ripgrep_finds(self):
        for git in (False, True):
            if git:
                subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
            for path in (".", "src", "src/a.py", "long.txt"):
                for pattern in ("alpha", "^beta", "x{3}", "nothing"):
                    with self.subTest(git=git, path=path, pattern=pattern):
                        expected = self.tools.grep_search(path, pattern)["output"]
                        actual = self.search(path, pattern)["output"]
                        self.assertEqual(sorted(actual.splitlines()), _ripgrep_lines(expected))
