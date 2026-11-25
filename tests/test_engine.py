import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from refl.client import THINKING_BUDGET_MESSAGE, ContextLimitError, ServerError, TokenLimitError
from refl.engine import TurnLimitError, _call_summary, attach_images, default_thinking_budget, tool_text

from .test_tools import png

from refl.challenge import CHALLENGE_PROMPT, REBUTTAL_PROMPT, SIMPLIFY_PROMPT, SIMPLIFY_REBUTTAL_PROMPT
from refl.tree import ConversationTree

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

    def test_main_branch_and_each_fork_send_their_own_session(self):
        engine, server, _ = make_engine(self.root, [
            text(_CLAIM),
            call("read_file", path="status.txt"),
            verdict(),
            text(f"Done. {_CLAIM} was verified."),
        ])
        asyncio.run(engine.respond("Check the status."))
        stem = engine.tree.path.stem
        sessions = [request["session"] for request in server.requests]
        self.assertEqual((sessions[0], sessions[3]), (stem, stem))
        self.assertEqual(sessions[1], sessions[2])
        start = int(sessions[1].removeprefix(f"{stem}-"))
        node = engine.tree.nodes[start]
        self.assertEqual(node.branch_type, "verification")
        self.assertTrue(node.message["content"].startswith("Use read_file or grep_search"))

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
        # The main branch, the reviewers with the whole conversation, and the brief ones get the dependency rule.
        rule = "Do not install packages or add dependencies."
        self.assertTrue(all(rule in request["system"] for request in server.requests
                            if not request["system"].startswith("Compare the exact claim")))
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

    def test_a_server_that_counts_the_reply_names_both_parts_and_how_to_continue(self):
        engine, _, _ = make_engine(self.root, [ContextLimitError(900, 1000, reply_tokens=256)], forks=False)
        with self.assertRaises(ContextLimitError) as raised:
            asyncio.run(engine.respond("hi"))
        self.assertEqual(raised.exception.reply_tokens, 256)
        message = str(raised.exception)
        self.assertTrue(message.startswith("The request needs 900 tokens plus 256 for the reply"))
        self.assertIn("a lower --max-tokens", message)
        self.assertIn(f"--resume {engine.tree.path.stem}", message)


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


# An edit of five lines is large enough to start a plan challenge.
_LARGE = "".join(f"line {number}\n" for number in range(5))


def _critic_route(objections, verdicts=None, rebuttal=None, explore=True, simpler="", simpler_rebuttal=None):
    """Answer the forks of a plan challenge by their content, so their order does not matter.

    `objections` maps a critic to its objection, `verdicts` a word in a finding to the verifier's status, and
    `rebuttal` gives the status of every objection's rebuttal. `simpler` is the simplifier's strategy, empty for
    none, and `simpler_rebuttal` the status of its rebuttal.
    """
    def route(request):
        system, schema = request["system"], (request["response_format"] or {}).get("schema", {})
        properties = schema.get("properties", {})
        if system.startswith((CHALLENGE_PROMPT, SIMPLIFY_PROMPT)) and request["tools"]:
            return call("read_file", path="status.txt") if explore else text("Nothing to read.")
        if "simpler_plan" in properties:
            return text(json.dumps({"goal": "status.txt says new_ready", "simpler_plan": simpler,
                                    "relies_on": "status.txt", "why_simpler": "one edit instead of a script",
                                    "requirements": "keeps line 1"}))
        if "status" in properties and system.startswith(SIMPLIFY_REBUTTAL_PROMPT):
            return text(json.dumps({"reason": "It kept the script without a reason.", "status": simpler_rebuttal}))
        if "objection" in properties:
            critic = system.split("Your role: ")[1].split(".")[0]
            return text(json.dumps({"objection": objections.get(critic, ""), "evidence": "status.txt line 1",
                                    "check": "read status.txt", "likelihood": "high"}))
        if system.startswith("You are Reflect's read-only verifier"):
            return call("read_file", path="status.txt")
        if "status" in properties and system.startswith(REBUTTAL_PROMPT):
            return text(json.dumps({"reason": "The plan still writes old_ready.", "status": rebuttal}))
        if "status" in properties:
            claim = request["messages"][1]["content"].split("\n\nCurrent evidence")[0]
            status = next(status for word, status in (verdicts or {}).items() if word in claim)
            return verdict(status, f"{status} by status.txt line 1")
        return None
    return route


class ChallengeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "status.txt").write_text("old_ready\n", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def test_proven_objections_reach_the_plan_and_get_one_rebuttal(self):
        route = _critic_route({"Requirements": "It keeps old_ready", "Environment": "Python is missing"},
                              verdicts={"old_ready": "supported", "Python": "contradicted"}, rebuttal="unresolved")
        replies = [call("write_file", path="app.py", content=_LARGE),
                   text("The requirement holds, so I will write new_ready."),
                   text("I will change status.txt first."),
                   call("write_file", path="app.py", content="print(2)\n"),
                   text("Done.")]
        # The challenge has its own requests, so three of the turn's are enough for the plan.
        engine, server, events = make_engine(self.root, replies, max_turns=3, challenge=True, route=route)
        self.assertEqual(asyncio.run(engine.respond("Write app.py.")), "Done.")
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "print(2)\n")

        messages = engine.tree.messages(engine.tree.head)
        contents = [str(message.get("content")) for message in messages]
        self.assertIn("Tool call deferred. Reissue it after reviewing the plan challenge.", contents[3])
        challenge = next(content for content in contents if content.startswith("Plan challenge"))
        self.assertIn("Proven objections\n- Requirements: (high) It keeps old_ready. Evidence: status.txt line 1. "
                      "Check: read status.txt. Verifier: supported by status.txt line 1", challenge)
        self.assertNotIn("Python is missing", challenge)
        self.assertTrue(any("Dropped the Environment objection" in event for event in events))
        unresolved = next(content for content in contents if content.startswith("Unresolved points"))
        self.assertIn("- Requirements: It keeps old_ready Still open: The plan still writes old_ready.", unresolved)
        self.assertEqual(contents.count("Carry out the revised plan now. Reissue the deferred calls that are still "
                                        "needed."), 1)

        # Each critic and the verifier and rebuttal of its objection have their own sessions.
        stem = engine.tree.path.stem
        forks = {request["session"] for request in server.requests if request["session"] != stem}
        # The critics and the simplifier, the verifiers of two objections, and the rebuttal of the proven one.
        self.assertEqual(len(forks), 4 + 2 + 1)
        # A saved session with challenge branches loads again.
        resumed = ConversationTree(engine.tree.path.parent, "system", resume=stem)
        self.assertEqual(len(resumed.nodes), len(engine.tree.nodes))

    def test_only_the_first_project_change_of_a_task_is_challenged(self):
        route = _critic_route({}, explore=False)
        replies = [call("write_file", path=".refl/skills/notes.md", content="# Notes\n"),
                   call("write_file", path="a.txt", content=_LARGE),
                   call("write_file", path="a.txt", content="a\n"),
                   call("write_file", path="b.txt", content=_LARGE),
                   text("Done.")]
        engine, _, events = make_engine(self.root, replies, challenge=True, route=route)
        self.assertEqual(asyncio.run(engine.respond("Write a and b.")), "Done.")
        self.assertEqual(sum(event.startswith("Challenge\nLooking for reasons") for event in events), 1)
        self.assertIn("Challenge\nNo objection or simpler strategy held up.", events)
        self.assertTrue((self.root / "b.txt").exists())

        engine.server.replies = [call("write_file", path="c.txt", content=_LARGE),
                                 call("write_file", path="c.txt", content="c\n"), text("Done.")]
        self.assertEqual(asyncio.run(engine.respond("Write c.")), "Done.")
        self.assertEqual(sum(event.startswith("Challenge\nLooking for reasons") for event in events), 2)

    def test_a_proven_simpler_strategy_must_be_taken_or_refused_for_a_reason(self):
        route = _critic_route({}, verdicts={"Edit line 1": "supported"}, simpler="Edit line 1 of status.txt",
                              simpler_rebuttal="unresolved")
        replies = [call("write_file", path="update.py", content=_LARGE),
                   text("I keep the script."),
                   text("I switch to the single edit."),
                   call("edit_file", path="status.txt", old_text="old_ready", new_text="new_ready"),
                   text("Done.")]
        engine, server, events = make_engine(self.root, replies, challenge=True, route=route)
        self.assertEqual(asyncio.run(engine.respond("Set the status to new_ready.")), "Done.")
        self.assertEqual((self.root / "status.txt").read_text(encoding="utf-8"), "new_ready\n")
        self.assertFalse((self.root / "update.py").exists())

        contents = [str(message.get("content")) for message in engine.tree.messages(engine.tree.head)]
        challenge = next(content for content in contents if content.startswith("Plan challenge"))
        self.assertIn("Simpler strategy, proven to keep the requirements\n- Simpler plan: Goal: status.txt says "
                      "new_ready. Simpler strategy: Edit line 1 of status.txt. Relies on: status.txt. Saves: one edit "
                      "instead of a script. Keeps: keeps line 1. Verifier: supported by status.txt line 1", challenge)
        # The verifier checks what the strategy rests on today, not a prediction that it will work.
        claim = next(event for event in events if event.startswith("Verifying The workspace has"))
        self.assertEqual(claim, "Verifying The workspace has what this strategy relies on, and it does what the "
                                "strategy needs: status.txt. The request and any spec or tests in the workspace set "
                                "no requirement beyond these: keeps line 1. Strategy: Edit line 1 of status.txt")
        self.assertIn("For the simpler strategy, say whether you switch to it.", challenge)
        self.assertNotIn("Answer each objection", challenge)
        unresolved = next(content for content in contents if content.startswith("Unresolved points"))
        self.assertIn("- Simpler plan: Edit line 1 of status.txt Still open: It kept the script without a reason.",
                      unresolved)
        # The simplifier states the goal before its strategy.
        schema = next(request["response_format"]["schema"] for request in server.requests
                      if request["response_format"] and "simpler_plan" in request["response_format"]["schema"]["properties"])
        self.assertEqual(schema["required"][:2], ["goal", "simpler_plan"])

    def test_a_simpler_strategy_that_misses_a_requirement_is_dropped(self):
        route = _critic_route({}, verdicts={"Delete": "contradicted"}, simpler="Delete status.txt")
        replies = [call("write_file", path="a.txt", content=_LARGE),
                   call("write_file", path="a.txt", content="a\n"), text("Done.")]
        engine, _, events = make_engine(self.root, replies, challenge=True, route=route)
        self.assertEqual(asyncio.run(engine.respond("Write a.")), "Done.")
        self.assertIn("Challenge\nDropped the simpler strategy: contradicted by status.txt line 1", events)
        self.assertIn("Challenge\nNo objection or simpler strategy held up.", events)

    def test_small_edits_are_not_challenged_and_keep_the_challenge_for_later(self):
        (self.root / "big.txt").write_text(_LARGE, encoding="utf-8")
        route = _critic_route({}, explore=False)
        replies = [call("edit_file", path="status.txt", old_text="old_ready", new_text="new_ready"),
                   call("write_file", path="new.txt", content="one line\n"),
                   # A few lines over a large file remove the rest of it, so they are not small.
                   call("write_file", path="big.txt", content="x\n"),
                   call("write_file", path="big.txt", content="x\n"),
                   text("Done.")]
        engine, _, events = make_engine(self.root, replies, challenge=True, route=route)
        self.assertEqual(asyncio.run(engine.respond("Update the files.")), "Done.")
        skipped = [event for event in events if event.startswith("Challenge\nSkipped for a small edit")]
        self.assertEqual(skipped, ["Challenge\nSkipped for a small edit: edit_file status.txt",
                                   "Challenge\nSkipped for a small edit: write_file new.txt"])
        self.assertEqual(sum(event.startswith("Challenge\nLooking for reasons") for event in events), 1)
        self.assertIn("Looking for reasons the plan fails, and for a simpler one, before its first change: "
                      "write_file big.txt", next(event for event in events if "Looking for reasons" in event))

    def test_what_counts_as_a_small_edit(self):
        from refl.challenge import small_edit
        self.assertTrue(small_edit("edit_file", {"old_text": "a\nb\nc", "new_text": "d"}, None))
        self.assertFalse(small_edit("edit_file", {"old_text": "a\nb\nc\nd", "new_text": "d"}, None))
        self.assertFalse(small_edit("edit_file", {"old_text": "a", "new_text": "x" * 201}, None))
        self.assertTrue(small_edit("write_file", {"content": "a\nb\n"}, None))
        self.assertTrue(small_edit("write_file", {"content": "a\n"}, "old\n"))
        self.assertFalse(small_edit("write_file", {"content": "a\n"}, _LARGE))
        self.assertFalse(small_edit("write_file", {"content": _LARGE}, None))
        self.assertFalse(small_edit("edit_file", {"old_text": None, "new_text": "a"}, None))

    def test_linear_runs_have_no_challenge(self):
        engine, _, events = make_engine(self.root, [call("write_file", path="a.txt", content="a\n"),
                                                    text("Done.")], forks=False, challenge=True)
        self.assertEqual(asyncio.run(engine.respond("Write a.")), "Done.")
        self.assertFalse(any(event.startswith("Challenge") for event in events))

    def test_a_challenge_stops_at_its_own_request_budget(self):
        route = _critic_route({"Requirements": "It keeps old_ready"}, verdicts={"old_ready": "supported"},
                              rebuttal="resolved", explore=False)
        # Five requests give the first critic its answer and its verifier one step. The third critic, the
        # verdict, and the main branch's answer find the budget used up, so the objection stays unproven.
        replies = [call("write_file", path="a.txt", content=_LARGE),
                   call("write_file", path="a.txt", content="a\n"), text("Done.")]
        engine, _, events = make_engine(self.root, replies, challenge=True, route=route)
        with mock.patch("refl.engine.CHALLENGE_REQUESTS", 5):
            self.assertEqual(asyncio.run(engine.respond("Write a.")), "Done.")
        self.assertIn("Challenge Edge cases: The plan challenge used all of its requests.", events)
        challenge = next(node.message["content"] for node in engine.tree.nodes
                         if str(node.message.get("content")).startswith("Plan challenge"))
        self.assertIn("Unproven objections\n- Requirements: (high) It keeps old_ready.", challenge)
        self.assertIn("Challenge\nNo answer to the objections: The plan challenge used all of its requests.", events)
        self.assertIsNone(engine.challenge_left)
        self.assertTrue((self.root / "a.txt").exists())

