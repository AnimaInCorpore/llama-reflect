import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from refl.client import THINKING_BUDGET_MESSAGE, ContextLimitError, ServerError, TokenLimitError
from refl.engine import TurnLimitError, _call_summary, attach_images, default_thinking_budget, tool_text

from .test_tools import png

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
        replies += [text("Read a.txt first.")] * 3 + [decision(), call("write_file", path="a.txt", content="b\n")]
        replies += [call("read_file", path="a.txt", start_line=1, end_line=line) for line in range(1, 4)]
        replies += [text("All checks pass.")]
        engine, server, events = make_engine(self.root, replies, max_turns=11)
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "All checks pass.")
        self.assertTrue(any(event.startswith("Strategy\nReflecting") for event in events))
        self.assertEqual(server.requests[-1]["tools"], ["bash"])
        self.assertIn("Run only the check: change no files", server.requests[-1]["messages"][-1]["content"])

    def test_a_task_without_changes_gets_its_answer_on_the_last_request(self):
        # A question that stalled must not end with a smoke check in place of its answer.
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt")]
        replies += [text("Read a.txt first.")] * 3 + [decision()]
        replies += [call("read_file", path="a.txt", start_line=1, end_line=line) for line in range(1, 5)]
        replies += [text("a.txt holds one line, a.")]
        engine, server, _ = make_engine(self.root, replies, max_turns=11)
        self.assertEqual(asyncio.run(engine.respond("What is in a.txt?")), "a.txt holds one line, a.")
        self.assertEqual(server.requests[-1]["tools"], [])
        self.assertTrue(server.requests[-1]["messages"][-1]["content"].startswith("This is the final request"))

        replies[-1] = call("bash", command="cat a.txt")
        engine, _, _ = make_engine(self.root, replies, max_turns=11)
        with self.assertRaisesRegex(ServerError, "Expected a text answer on the final request"):
            asyncio.run(engine.respond("What is in a.txt?"))

    def test_a_turn_that_runs_out_reports_where_it_stands(self):
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        reads = [call("read_file", path="a.txt", start_line=1, end_line=line) for line in range(1, 5)]
        engine, server, _ = make_engine(self.root, reads + [text("Read a.txt. Next: port the input map.")],
                                        max_turns=4)
        with self.assertRaises(TurnLimitError) as raised:
            asyncio.run(engine.respond("Port it."))
        self.assertEqual(raised.exception.report, "Read a.txt. Next: port the input map.")
        self.assertIn('Send a follow-up such as "continue"', str(raised.exception))
        # The report comes on top of the limit, without tools, so the work kept all four requests.
        self.assertEqual((len(server.requests), server.requests[-1]["tools"]), (5, []))
        self.assertTrue(server.requests[-1]["messages"][-1]["content"].startswith("The request limit of this turn"))
        last = engine.tree.nodes[engine.tree.head].message
        self.assertEqual(last["role"], "assistant")
        self.assertTrue(last["content"].startswith("Read a.txt. Next: port the input map.\n\n[Turn stopped] Reached"))

        engine, _, _ = make_engine(self.root, reads + [ServerError("HTTP 500")], max_turns=4)
        with self.assertRaises(TurnLimitError) as raised:
            asyncio.run(engine.respond("Port it."))
        self.assertIsNone(raised.exception.report)

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

        explore = [request for request in server.requests if request["tools"] == ["read_file", "grep_search"]]
        self.assertIn("Do not assume anything that no tool result", explore[0]["system"])
        self.assertEqual([request["tool_choice"] for request in explore], ["required", "required"])
        choice = next(request for request in server.requests if request["response_format"]
                      and "role" in request["response_format"]["schema"]["properties"])
        self.assertEqual(choice["tools"], [])
        self.assertEqual(list(choice["response_format"]["schema"]["properties"]), ["reason", "first_step", "role"])
        self.assertEqual(choice["response_format"]["schema"]["properties"]["role"]["enum"],
                         ["Diagnosis", "Observe", "Switch tools"])
        main = [node.message["content"] for node in engine.tree.nodes if node.branch_type == "main"]
        self.assertIn("Plan: try the Observe idea first. Reason: It explains the failures. "
                      "First step: Run the check.", main)
        self.assertEqual(main[main.index(next(m for m in main if str(m).startswith("Plan:"))) + 1],
                         "Carry out the plan now, starting with its first step.")

    def test_final_answer_while_the_check_fails_starts_reflection(self):
        check = "echo FAILED; exit 1"
        idea = text(json.dumps({"proposal": "Run it with -v", "basis": "more output", "cost": "one command",
                                "fails_if": "no new output"}))
        replies = [call("bash", command=check), call("write_file", path="a.txt", content="x"),
                   call("bash", command=check), text("The environment must be broken."),
                   text("Read the imported module."), text("Print sys.path."), idea, decision("Diagnosis"),
                   text("Done.")]
        engine, server, events = make_engine(self.root, replies, verification=False)
        self.assertEqual(asyncio.run(engine.respond("Make the check pass.")), "Done.")
        self.assertFalse(server.replies)
        main = [str(node.message.get("content")) for node in engine.tree.nodes if node.branch_type == "main"]
        stop = main.index("The environment must be broken.")
        self.assertTrue(main[stop + 1].startswith(
            f"Reflection results\nObserved stall: The main branch gave a final answer while {check} still fails."))

    def test_final_answer_after_one_failed_command_ends_the_turn(self):
        engine, server, _ = make_engine(self.root, [call("bash", command="command -v nothere"),
                                                    text("It is not installed.")], verification=False)
        self.assertEqual(asyncio.run(engine.respond("Is nothere installed?")), "It is not installed.")
        self.assertEqual(len(server.requests), 2)

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
            call("edit_file", path="a.txt", old_text="beta", new_text='say("gamma")'),
            text("ok"),
        ])
        asyncio.run(engine.respond("Write a.txt."))
        brief = engine._brief(engine.tree.head, "A reason.")
        self.assertIn("- write_file a.txt (content: alpha\\nbeta)\n  Wrote a.txt\n", brief)
        self.assertIn('- edit_file a.txt (replaced "beta" with "say("gamma")")\n  Edited a.txt', brief)

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

    def test_tool_results_reach_the_model_as_plain_text(self):
        (self.root / "quote.py").write_text('print("hi")\n', encoding="utf-8")
        engine, server, _ = make_engine(self.root, [
            call("read_file", path="quote.py"),
            call("bash", command="exit 3"),
            text("Done."),
        ], forks=False)
        asyncio.run(engine.respond("Read quote.py."))
        sent = [message["content"] for message in server.requests[-1]["messages"] if message["role"] == "tool"]
        self.assertEqual(sent[0], '1: print("hi")')
        self.assertTrue(sent[1].startswith("Exit code: 3"))
        stored = [node.message["content"] for node in engine.tree.nodes if node.message["role"] == "tool"]
        self.assertEqual(json.loads(stored[0]), {"output": '1: print("hi")'})

    def test_tool_text(self):
        self.assertEqual(tool_text(json.dumps({"error": "Bash timed out", "output": "partial"})),
                         "Error: Bash timed out\npartial")
        self.assertEqual(tool_text(json.dumps({"output": "Edited a.txt", "changed": True})), "Edited a.txt")
        unknown = json.dumps({"rows": 3})
        self.assertEqual(tool_text(unknown), unknown)
        self.assertEqual(tool_text("plain"), "plain")

    def test_text_and_calls_are_split_only_for_templates_that_move_text(self):
        planned = {**call("read_file", path="status.txt"), "content": "I will read status.txt."}
        for moves_text, split in ((True, True), (False, False), (None, False)):
            engine, server, _ = make_engine(self.root, [dict(planned), text("Done.")], forks=False,
                                            moves_text=moves_text)
            asyncio.run(engine.respond("Check the status."))
            assistant = [message for message in server.requests[-1]["messages"] if message["role"] == "assistant"]
            if split:
                self.assertEqual([message["content"] for message in assistant], ["I will read status.txt.", None])
                self.assertEqual([bool(message.get("tool_calls")) for message in assistant], [False, True])
            else:
                self.assertEqual(len(assistant), 1, moves_text)
            self.assertEqual(server.template_requests, 1)
            stored = next(node.message for node in engine.tree.nodes if node.message["role"] == "assistant")
            self.assertEqual((stored["content"], len(stored["tool_calls"])), ("I will read status.txt.", 1))

    def test_context_use_is_reported_once_per_threshold(self):
        reads = [call("read_file", path="status.txt") for _ in range(3)]
        engine, _, events = make_engine(self.root, [*reads, text("Done.")], forks=False,
                                        tokens=[500, 760, 800, 950], context_size=1000)
        self.assertEqual(asyncio.run(engine.respond("Read the status.")), "Done.")
        warnings = [event for event in events if event.startswith("Context:")]
        self.assertEqual([warning.split(". ")[0] for warning in warnings],
                         ["Context: 760 of 1,000 tokens in use (76%)", "Context: 950 of 1,000 tokens in use (95%)"])
        self.assertIn(f"--resume {engine.tree.path.stem}", warnings[0])

    def test_unknown_context_size_shows_no_warning(self):
        engine, _, events = make_engine(self.root, [text("Done.")], forks=False, tokens=[990])
        asyncio.run(engine.respond("hi"))
        self.assertFalse(any(event.startswith("Context:") for event in events))

    def test_a_request_that_does_not_fit_explains_how_to_continue(self):
        engine, _, _ = make_engine(self.root, [ContextLimitError(1200, 1000)], forks=False)
        with self.assertRaises(ContextLimitError) as raised:
            asyncio.run(engine.respond("hi"))
        message = str(raised.exception)
        self.assertTrue(message.startswith(
            "The request needs 1,200 tokens, but llama-server holds 1,000 tokens of context per slot."))
        self.assertIn(f"--resume {engine.tree.path.stem}", message)
        self.assertEqual(engine.tree.messages(engine.tree.head)[-1]["content"], f"[Turn stopped] {message}")


    def test_default_thinking_budget_leaves_half_for_the_answer(self):
        self.assertEqual([default_thinking_budget(tokens) for tokens in (256, 2048, 8192)], [128, 1024, 4096])

    def test_a_cut_off_reply_is_retried_once_with_a_note(self):
        engine, server, events = make_engine(self.root, [
            TokenLimitError("Completion reached its token limit. Increase --max-tokens."),
            call("write_file", path="a.svg", content="<svg/>"),
            text("Done."),
        ], forks=False)
        self.assertEqual(asyncio.run(engine.respond("Draw a pelican.")), "Done.")
        note = server.requests[1]["messages"][-1]
        self.assertEqual(note["role"], "user")
        self.assertTrue(note["content"].startswith("Your last reply reached the limit of 256 output tokens"))
        self.assertIn("The reply reached the 256-token limit. Asking for a shorter one.", events)
        self.assertEqual((self.root / "a.svg").read_text(encoding="utf-8"), "<svg/>")

    def test_a_second_cut_off_reply_stops_the_turn(self):
        limit = TokenLimitError("Completion reached its token limit. Increase --max-tokens.")
        engine, _, _ = make_engine(self.root, [limit, limit], forks=False)
        with self.assertRaises(TokenLimitError):
            asyncio.run(engine.respond("Draw a pelican."))

    def test_thinking_budget_reaches_the_server(self):
        engine, server, _ = make_engine(self.root, [text("Done.")], forks=False)
        asyncio.run(engine.respond("hi"))
        self.assertEqual(server.requests[0]["thinking_budget"], default_thinking_budget(256))
        engine, server, _ = make_engine(self.root, [text("Done.")], forks=False, thinking_budget=-1)
        asyncio.run(engine.respond("hi"))
        self.assertIsNone(server.requests[0]["thinking_budget"])

    def test_a_thinking_block_cut_at_its_budget_is_reported(self):
        cut = {**text("Done."), "reasoning_content": f"Let me think about it.\n\n{THINKING_BUDGET_MESSAGE}\n"}
        engine, _, events = make_engine(self.root, [cut], forks=False, thinking_budget=100)
        self.assertEqual(asyncio.run(engine.respond("hi")), "Done.")
        self.assertIn("Thinking stopped at its budget of 100 tokens", events)

    def test_fork_effort_applies_to_verifiers_only(self):
        engine, server, _ = make_engine(self.root, [
            text(_CLAIM),
            call("read_file", path="status.txt"),
            verdict(),
            text(f"Done. {_CLAIM} was verified."),
        ], fork_effort="low")
        asyncio.run(engine.respond("Check the status."))
        self.assertEqual([request["reasoning_effort"] for request in server.requests], [None, "low", "low", None])

    def test_view_image_shows_the_image_after_the_tool_result(self):
        (self.root / "square.png").write_bytes(png(8, 8))
        engine, server, _ = make_engine(self.root, [call("view_image", path="square.png"), text("It is red.")],
                                        forks=False, vision=True)
        self.assertEqual(asyncio.run(engine.respond("What colour is square.png?")), "It is red.")
        self.assertIn("view_image", server.requests[0]["tools"])
        tool, image = server.requests[1]["messages"][-2:]
        self.assertEqual(tool["role"], "tool")
        self.assertTrue(tool["content"].startswith("Showing square.png (PNG, 8 x 8 pixels,"))
        self.assertEqual(image["role"], "user")
        self.assertEqual(image["content"][0], {"type": "text", "text": "The image from the view_image call above."})
        self.assertTrue(image["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_without_image_input_the_tool_is_not_offered(self):
        engine, server, _ = make_engine(self.root, [text("Done.")], forks=False)
        asyncio.run(engine.respond("hi"))
        self.assertNotIn("view_image", server.requests[0]["tools"])
        self.assertIn("read_file", server.requests[0]["tools"])

    def test_images_follow_the_last_tool_result_of_a_reply(self):
        image = json.dumps({"output": "Showing a.png", "image": "data:image/png;base64,AAAA"})
        messages = [
            {"role": "assistant", "content": None, "tool_calls": []},
            {"role": "tool", "tool_call_id": "1", "content": image},
            {"role": "tool", "tool_call_id": "2", "content": image},
            {"role": "user", "content": "Next."},
        ]
        shown = attach_images(messages, vision=True)
        self.assertEqual([message["role"] for message in shown], ["assistant", "tool", "tool", "user", "user"])
        self.assertEqual(shown[1]["content"], "Showing a.png")
        self.assertEqual(shown[3]["content"][0]["text"], "The 2 images from the view_image calls above, in order.")
        self.assertEqual(len(shown[3]["content"]), 3)
        hidden = attach_images(messages, vision=False)
        self.assertEqual(hidden[3]["content"],
                         "The server has no image input, so the images from view_image are not shown.")

if __name__ == "__main__":
    unittest.main()
