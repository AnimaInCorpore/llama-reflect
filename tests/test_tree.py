import tempfile
import unittest
from pathlib import Path

from refl.tree import ConversationTree


class TreeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.sessions = Path(self.directory.name) / ".refl" / "sessions"

    def tearDown(self):
        self.directory.cleanup()

    def test_long_main_branch_stays_flat(self):
        tree = ConversationTree(self.sessions, "system", persist=False)
        for index in range(60):
            tree.append({"role": "user" if index % 2 else "assistant", "content": f"message {index}"})
        lines = tree.render().splitlines()
        self.assertEqual(len(lines), 61)
        self.assertTrue(all(line[0].isdigit() for line in lines))

    def test_forks_are_indented_and_main_continues(self):
        tree = ConversationTree(self.sessions, "system", persist=False)
        tree.append({"role": "user", "content": "task"})
        anchor = tree.append({"role": "assistant", "content": "<verify>claim</verify>"})
        fork = tree.append({"role": "user", "content": "check"}, parent_id=anchor, branch_type="verification")
        tree.append({"role": "assistant", "content": "verdict"}, parent_id=fork, branch_type="verification")
        tree.append({"role": "user", "content": "Verification results"})
        lines = tree.render().splitlines()
        self.assertTrue(lines[3].startswith("+-- 3 verification/user"))
        self.assertTrue(lines[4].startswith("|   4 verification/assistant"))
        self.assertTrue(lines[5].startswith("5 main/user (active) [current]"))

    def test_session_directory_is_git_ignored(self):
        ConversationTree(self.sessions, "system")
        self.assertEqual((self.sessions / ".gitignore").read_text(encoding="utf-8"), "*\n")

    def test_existing_ignore_file_is_kept(self):
        self.sessions.mkdir(parents=True)
        (self.sessions / ".gitignore").write_text("custom\n", encoding="utf-8")
        ConversationTree(self.sessions, "system")
        self.assertEqual((self.sessions / ".gitignore").read_text(encoding="utf-8"), "custom\n")


if __name__ == "__main__":
    unittest.main()
