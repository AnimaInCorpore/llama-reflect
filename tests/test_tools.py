import tempfile
import unittest
from pathlib import Path

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
