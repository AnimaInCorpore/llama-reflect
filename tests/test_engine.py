import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from refl.engine import _call_summary

from .fakes import call, decision, make_engine, text, verdict

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
        replies += [text("Read a.txt first.")] * 3 + [decision()]
        replies += [call("read_file", path="a.txt", start_line=1, end_line=line) for line in range(1, 5)]
        replies += [text("All checks pass.")]
        engine, server, events = make_engine(self.root, replies, max_turns=11)
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

    def test_no_verify_keeps_reflection(self):
        engine, server, events = make_engine(self.root, [
            text(f"Answer with a tag: {_CLAIM}"),
        ], verification=False)
        self.assertTrue(asyncio.run(engine.respond("hi")).startswith("Answer with a tag"))
        self.assertNotIn("<verify>", server.requests[0]["system"])
        self.assertFalse(any(event.startswith("Verifying") for event in events))

        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt")]
        replies += [text("Read status.txt.")] * 3 + [decision(), text("Done.")]
        engine, _, events = make_engine(self.root, replies, verification=False)
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "Done.")
        self.assertTrue(any(event.startswith("Strategy\nReflecting") for event in events))

    def test_reflection_roles_and_brief(self):
        idea = text(json.dumps({"proposal": "Edit the bytes with Python", "basis": "UTF-8 tools fail",
                                "cost": "one command", "fails_if": "the file stays unchanged"}))
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt"),
                   text("Check whether missing.txt exists."), text("Print the directory listing."), idea,
                   decision("Observe"), text("Done.")]
        engine, server, events = make_engine(self.root, replies)
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "Done.")
        self.assertIn("Temperature 0: Diagnosis, Observe, Switch tools", next(e for e in events if "Reflecting" in e))

        brief = next(request for request in server.requests if request["response_format"])
        self.assertEqual(len(brief["messages"]), 2)
        self.assertNotIn("You are Reflect", brief["system"])
        self.assertIn("Your role: Switch tools.", brief["system"])
        self.assertEqual(brief["temperature"], 0.3)
        summary = brief["messages"][1]["content"]
        self.assertIn("Task: Fix it.", summary)
        self.assertIn("- read_file missing.txt", summary)
        self.assertIn("status.txt", summary)

        results = next(node.message["content"] for node in engine.tree.nodes
                       if str(node.message.get("content", "")).startswith("Reflection results"))
        self.assertIn("Evidence-based ideas\n- Diagnosis: Check whether missing.txt exists.\n"
                      "- Observe: Print the directory listing.\n\nUnverified ideas\n"
                      "- Switch tools: Edit the bytes with Python. Basis: UTF-8 tools fail. Cost: one command. "
                      "Fails if: the file stays unchanged.", results)

        choice = next(request for request in server.requests if request["response_format"]
                      and "role" in request["response_format"]["schema"]["properties"])
        self.assertEqual(choice["tools"], [])
        self.assertEqual(choice["response_format"]["schema"]["properties"]["role"]["enum"],
                         ["Diagnosis", "Observe", "Switch tools"])
        main = [node.message["content"] for node in engine.tree.nodes if node.branch_type == "main"]
        self.assertIn("Plan: try the Observe idea first. Reason: It explains the failures. "
                      "First step: Run the check.", main)
        self.assertEqual(main[main.index(next(m for m in main if str(m).startswith("Plan:"))) + 1],
                         "Carry out the plan now, starting with its first step.")

    def test_unusable_decision_still_continues(self):
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt")]
        replies += [text("Read status.txt.")] * 3 + [text("not json"), text("Done.")]
        engine, _, events = make_engine(self.root, replies)
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "Done.")
        main = [node.message["content"] for node in engine.tree.nodes if node.branch_type == "main"]
        self.assertIn("I will follow the reflection results.", main)
        self.assertTrue(any(event.startswith("Strategy\nNo plan after reflection") for event in events))

    def test_brief_shows_what_writes_changed(self):
        engine, _, _ = make_engine(self.root, [
            call("write_file", path="a.txt", content="alpha\nbeta"),
            call("edit_file", path="a.txt", old_text="beta", new_text="gamma"),
            text("ok"),
        ])
        asyncio.run(engine.respond("Write a.txt."))
        brief = engine._brief(engine.tree.head, "A reason.")
        self.assertIn("- write_file a.txt (content: alpha\\nbeta)\n", brief)
        self.assertIn('- edit_file a.txt (replaced "beta" with "gamma")\n', brief)

    def test_thinking_modes(self):
        replies = [text(_CLAIM), call("read_file", path="status.txt"), verdict(), text("Done.")]
        for mode, main_thinks, fork_thinks in (("auto", True, True), ("main", True, False), ("off", False, False)):
            engine, server, _ = make_engine(self.root, list(replies), thinking=mode)
            asyncio.run(engine.respond("Check the status."))
            main = [request["thinking"] for request in server.requests if request["system"].startswith("You are Reflect,")]
            forks = [request["thinking"] for request in server.requests if not request["system"].startswith("You are Reflect,")]
            self.assertEqual((set(main), set(forks)), ({main_thinks}, {fork_thinks}), mode)

    def test_strategy_log_is_git_ignored(self):
        engine, _, _ = make_engine(self.root, [])
        engine._strategy_event("## Reflection")
        strategies = self.root / ".refl" / "strategies"
        self.assertEqual((strategies / ".gitignore").read_text(encoding="utf-8"), "*\n")
        self.assertTrue((self.root / engine.strategy_path).is_file())


if __name__ == "__main__":
    unittest.main()
