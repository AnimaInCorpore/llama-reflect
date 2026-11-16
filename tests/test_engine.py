import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from refl.engine import _call_summary

from .fakes import call, make_engine, text, verdict

_CLAIM = "<verify>status.txt contains old_ready</verify>"


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "status.txt").write_text("old_ready\n", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def test_restated_claim_reuses_its_verdict_and_ends_the_turn(self):
        engine, server, events = make_engine(self.root, [
            text(_CLAIM),
            call("read_file", path="status.txt"),
            verdict(),
            text(f"Done. {_CLAIM} was verified."),
        ])
        answer = asyncio.run(engine.respond("Check the status."))
        self.assertTrue(answer.startswith("Done."))
        self.assertEqual(sum(event.startswith("Verifying") for event in events), 1)
        self.assertEqual(server.replies, [])

    def test_claim_is_checked_again_after_a_write(self):
        engine, server, events = make_engine(self.root, [
            text(_CLAIM),
            call("read_file", path="status.txt"),
            verdict(),
            call("write_file", path="status.txt", content="new_ready\n"),
            text(_CLAIM),
            call("read_file", path="status.txt"),
            verdict("contradicted", "status.txt line 1 contains new_ready"),
            text("Final."),
        ])
        self.assertEqual(asyncio.run(engine.respond("Update the status.")), "Final.")
        self.assertEqual(sum(event.startswith("Verifying") for event in events), 2)

    def test_smoke_check_request_answered_in_text(self):
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt")]
        replies += [text("Read a.txt first.")] * 3
        replies += [call("read_file", path="a.txt", start_line=1, end_line=line) for line in range(1, 5)]
        replies += [text("All checks pass.")]
        engine, server, events = make_engine(self.root, replies, max_turns=10)
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "All checks pass.")
        self.assertTrue(any(event.startswith("Strategy\nReflecting") for event in events))
        self.assertEqual(server.requests[-1]["tools"], ["bash"])

    def test_terminal_shows_the_tool_target(self):
        engine, _, events = make_engine(self.root, [call("read_file", path="status.txt"), text("ok")])
        asyncio.run(engine.respond("Read the status."))
        self.assertIn("read_file status.txt", events)

    def test_call_summary(self):
        bash = {"name": "bash", "arguments": json.dumps({"command": "pytest -x\n  tests"})}
        grep = {"name": "grep_search", "arguments": json.dumps({"pattern": "def main", "path": "refl"})}
        self.assertEqual(_call_summary(bash), "bash pytest -x tests")
        self.assertEqual(_call_summary(grep), 'grep_search "def main" refl')
        self.assertEqual(len(_call_summary({"name": "bash", "arguments": json.dumps({"command": "x" * 500})})), 200)

    def test_linear_mode_ignores_the_stored_prompt(self):
        engine, server, _ = make_engine(self.root, [text("ok")], forks=False)
        engine.tree.nodes[0].message["content"] = "Old prompt. Emit <verify>claim</verify> tags."
        asyncio.run(engine.respond("hi"))
        self.assertNotIn("<verify>", server.requests[0]["system"])

        engine, server, _ = make_engine(self.root, [text("ok")])
        asyncio.run(engine.respond("hi"))
        self.assertIn("<verify>", server.requests[0]["system"])

    def test_strategy_log_is_git_ignored(self):
        engine, _, _ = make_engine(self.root, [])
        engine._strategy_event("## Reflection")
        strategies = self.root / ".refl" / "strategies"
        self.assertEqual((strategies / ".gitignore").read_text(encoding="utf-8"), "*\n")
        self.assertTrue((self.root / engine.strategy_path).is_file())


if __name__ == "__main__":
    unittest.main()
