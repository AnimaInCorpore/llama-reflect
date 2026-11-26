import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

import history  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@unittest.skipUnless(shutil.which("git"), "needs git")
class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.directory.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "calc.py").write_text("def double(x):\n    return x\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "Start")
        (self.repo / "calc.py").write_text("def double(x):\n    return 2 * x\n", encoding="utf-8")
        (self.repo / "test_calc.py").write_text(
            "import unittest\nfrom calc import double\n\n\nclass T(unittest.TestCase):\n"
            "    def test_double(self):\n        self.assertEqual(double(2), 4)\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "Double the value\n\ndouble returned its argument.")
        self.commit = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--short", "HEAD"],
                                     capture_output=True, encoding="utf-8").stdout.strip()
        # An untracked input, such as a ROM set, that the checks need.
        (self.repo / "roms").mkdir()
        (self.repo / "roms" / "game.bin").write_bytes(b"\x01\x02")
        self.spec = {"name": "double", "commit": self.commit, "check": "python -m unittest test_calc",
                     "tests": ["test_calc.py"]}
        self.data = {"copy": ["roms"], "tasks": [self.spec]}

    def tearDown(self):
        self.directory.cleanup()

    def test_candidates_pair_a_change_with_its_test(self):
        found = history.candidates(self.repo)
        self.assertEqual([(sha, subject) for sha, subject, _ in found], [(self.commit, "Double the value")])

    def test_the_prompt_is_the_commit_message_and_its_check(self):
        self.assertEqual(history.prompt_of(self.repo, self.spec),
                         "Double the value\n\ndouble returned its argument.\n\nThe change is done when "
                         "`python -m unittest test_calc` passes. The tests in `test_calc.py` describe the expected "
                         "behavior. Do not change them.")

    def test_a_workspace_starts_at_the_parent_with_the_tests_and_inputs(self):
        workspace = Path(self.directory.name) / "out" / "ws"
        history.prepare(self.repo, self.data, self.spec, workspace)
        self.assertIn("return x", (workspace / "calc.py").read_text(encoding="utf-8"))
        self.assertTrue((workspace / "test_calc.py").is_file())
        self.assertEqual((workspace / "roms" / "game.bin").read_bytes(), b"\x01\x02")
        self.assertFalse(history.make_task(self.repo, self.spec).check(workspace)[0])
        (workspace / "calc.py").write_text("def double(x):\n    return 2 * x\n", encoding="utf-8")
        # Grading restores the commit's tests, so editing them does not help.
        (workspace / "test_calc.py").write_text("", encoding="utf-8")
        passed, detail = history.make_task(self.repo, self.spec).check(workspace)
        self.assertTrue(passed, detail)
        history.remove(workspace, self.data)
        self.assertFalse(workspace.exists())

    def test_removing_a_workspace_keeps_linked_inputs(self):
        workspace = Path(self.directory.name) / "out" / "linked"
        history.prepare(self.repo, self.data, self.spec, workspace, link=True)
        self.assertEqual((workspace / "roms" / "game.bin").read_bytes(), b"\x01\x02")
        history.remove(workspace, self.data)
        self.assertFalse(workspace.exists())
        self.assertEqual((self.repo / "roms" / "game.bin").read_bytes(), b"\x01\x02")

    def test_validation_needs_a_failing_parent_and_a_passing_commit(self):
        out = Path(self.directory.name) / "out"
        self.assertEqual(history.validate(self.repo, self.data, [self.spec], out), 0)
        broken = {**self.spec, "check": "true"}
        self.assertEqual(history.validate(self.repo, self.data, [broken], out), 1)


if __name__ == "__main__":
    unittest.main()
