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
