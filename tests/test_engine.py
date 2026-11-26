import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from refl.client import THINKING_BUDGET_MESSAGE, ContextLimitError, ServerError, TokenLimitError
from refl.engine import TurnLimitError, _call_summary, attach_images, default_thinking_budget, tool_text

from .test_tools import png

from refl.reflection import REFLECTION_PROMPT
from refl.challenge import CHALLENGE_PROMPT, SIMPLIFY_PROMPT
from refl.moderator import CHECK_PROMPT, CHOICE_PROMPT, MODERATOR_PROMPT, RULING_PROMPT
from refl.tree import ConversationTree

from .fakes import call, make_engine, reflection_route, text, verdict


def _choose(route, role):
    """Have the moderator of a reflection route choose `role`."""
    def chosen(request):
        if request["system"].startswith(MODERATOR_PROMPT) and CHOICE_PROMPT in request["system"]:
            return text(json.dumps({"reason": "a.txt line 1 explains the stall", "first_step": "Run the check.",
                                    "role": role}))
        return route(request)
    return chosen

_CLAIM = "<verify>status.txt contains old_ready</verify>"


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "status.txt").write_text("old_ready\n", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def test_the_main_branchs_thinking_is_shown_when_its_reply_arrives(self):
        engine, _, events = make_engine(self.root, [
            {"role": "assistant", "content": "Done.", "reasoning_content": "The user wants a short answer.\n"},
        ])
        self.assertEqual(asyncio.run(engine.respond("hi")), "Done.")
        self.assertIn("Thinking\nThe user wants a short answer.", events)

    def test_the_agent_tells_the_user_what_it_does(self):
        engine, server, events = make_engine(self.root, [
            {"role": "assistant", "content": "I will read the status first.\nThen I answer.",
             "tool_calls": call("read_file", path="status.txt")["tool_calls"]},
            text("It says old_ready."),
        ])
        self.assertEqual(asyncio.run(engine.respond("What is the status?")), "It says old_ready.")
        self.assertIn("» I will read the status first.\n  Then I answer.", events)
        self.assertIn("Talk to the user while you work", server.requests[0]["system"])

    def test_a_check_ends_with_one_summary_line(self):
        engine, _, events = make_engine(self.root, [
            text(f"I check first. {_CLAIM}"), call("read_file", path="status.txt"), verdict(), text("Done."),
        ])
        self.assertEqual(asyncio.run(engine.respond("Check the status.")), "Done.")
        self.assertIn("Check: verified: status.txt contains old_ready", events)
        # Only words beside tool calls are narration. A reply with a tag and no calls is not shown as chat.
        self.assertFalse(any(event.startswith("» ") for event in events))

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

    def test_a_turn_that_ends_with_the_smoke_check_also_reports(self):
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt")]
        replies += [call("write_file", path="a.txt", content="b\n")]
        replies += [call("read_file", path="a.txt", start_line=1, end_line=line) for line in range(1, 4)]
        replies += [call("bash", command="cat a.txt"), text("Fixed a.txt. Nothing is open.")]
        engine, server, _ = make_engine(self.root, replies, max_turns=7, route=reflection_route())
        answer = asyncio.run(engine.respond("Fix it."))
        self.assertEqual(answer, "Fixed a.txt. Nothing is open.\n\nSmoke check exited with code 0.\nb")
        self.assertTrue(server.requests[-1]["messages"][-1]["content"].startswith("The request limit"))
        self.assertEqual(server.requests[-1]["tools"], [])

    def test_the_answer_is_kept_for_the_next_session(self):
        engine, server, _ = make_engine(self.root, [text("The beach renders."), text("Picking up.")])
        self.assertEqual(asyncio.run(engine.respond("Fix the beach.")), "The beach renders.")
        report = (engine.tree.path.parent / "last-report.md").read_text(encoding="utf-8")
        self.assertEqual(report, f"# Last report\n\nSession: {engine.tree.path.stem}\n\nRequest: Fix the beach.\n\n"
                                 "The beach renders.\n")
        self.assertNotIn("last-report.md", server.requests[0]["system"])
        asyncio.run(engine.respond("resume"))
        self.assertIn("is in .refl/sessions/last-report.md. Read it first when the user asks to resume",
                      server.requests[1]["system"])

    def test_forks_think_with_a_budget_of_their_own(self):
        engine, server, _ = make_engine(self.root, [text(_CLAIM), call("read_file", path="status.txt"), verdict(),
                                                    text("Done.")], thinking_budget=10000)
        asyncio.run(engine.respond("Check."))
        budgets = [request["thinking_budget"] for request in server.requests]
        self.assertEqual(budgets, [10000, 4096, 4096, 10000])
        for main, fork, expected in ((100, None, 100), (10000, 500, 500), (10000, -1, None)):
            engine, server, _ = make_engine(self.root, [text(_CLAIM), call("read_file", path="status.txt"),
                                                        verdict(), text("Done.")],
                                            thinking_budget=main, fork_thinking_budget=fork)
            asyncio.run(engine.respond("Check."))
            self.assertEqual(server.requests[1]["thinking_budget"], expected)

    def test_reads_in_a_held_back_reply_run_now(self):
        engine, server, _ = make_engine(self.root, [
            {"role": "assistant", "content": _CLAIM, "tool_calls": [
                *call("read_file", path="status.txt")["tool_calls"],
                *call("write_file", path="status.txt", content="new_ready\n")["tool_calls"]]},
            call("read_file", path="status.txt"), verdict(),
            call("write_file", path="status.txt", content="new_ready\n"), text("Done."),
        ])
        self.assertEqual(asyncio.run(engine.respond("Update the status.")), "Done.")
        results = [node.message["content"] for node in engine.tree.nodes
                   if node.branch_type == "main" and node.message["role"] == "tool"][:2]
        self.assertIn("old_ready", results[0])
        self.assertIn("Tool call deferred", results[1])

    def test_smoke_check_request_answered_in_text(self):
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt")]
        replies += [call("write_file", path="a.txt", content="b\n")]
        replies += [call("read_file", path="a.txt", start_line=1, end_line=line) for line in range(1, 4)]
        replies += [text("All checks pass.")]
        engine, server, events = make_engine(self.root, replies, max_turns=7, route=reflection_route())
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "All checks pass.")
        self.assertTrue(any(event.startswith("Strategy\nReflecting") for event in events))
        self.assertEqual(server.requests[-1]["tools"], ["bash"])
        self.assertIn("Run only the check: change no files", server.requests[-1]["messages"][-1]["content"])

    def test_a_task_without_changes_gets_its_answer_on_the_last_request(self):
        # A question that stalled must not end with a smoke check in place of its answer.
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt")]
        replies += [call("read_file", path="a.txt", start_line=1, end_line=line) for line in range(1, 5)]
        replies += [text("a.txt holds one line, a.")]
        engine, server, _ = make_engine(self.root, replies, max_turns=7, route=reflection_route())
        self.assertEqual(asyncio.run(engine.respond("What is in a.txt?")), "a.txt holds one line, a.")
        self.assertEqual(server.requests[-1]["tools"], [])
        self.assertTrue(server.requests[-1]["messages"][-1]["content"].startswith("This is the final request"))

        replies[-1] = call("bash", command="cat a.txt")
        engine, _, _ = make_engine(self.root, replies, max_turns=7, route=reflection_route())
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
        replies += [text("Done.")]
        engine, _, events = make_engine(self.root, replies, verification=False, route=reflection_route())
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "Done.")
        self.assertTrue(any(event.startswith("Strategy\nReflecting") for event in events))

    def test_reflection_roles_and_brief(self):
        route = reflection_route({"Diagnosis": "Check whether missing.txt exists",
                                  "Observe": "Print the directory listing",
                                  "Switch tools": "Edit the bytes with Python"},
                                 facts="status.txt line 1 says old_ready")
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt"),
                   text("Done.")]
        engine, server, events = make_engine(self.root, replies, route=_choose(route, "Observe"))
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "Done.")
        self.assertIn("Temperature 0: Diagnosis, Observe, Switch tools", next(e for e in events if "Reflecting" in e))

        # Switch tools sees a brief instead of the whole conversation, and inspects the workspace like the rest.
        brief = next(request for request in server.requests
                     if "Your role: Switch tools." in request["system"] and request["tools"])
        self.assertEqual(len(brief["messages"]), 2)
        self.assertNotIn("You are Reflect", brief["system"])
        self.assertIn("Your role: Switch tools.", brief["system"])
        # The main branch, the reviewers with the whole conversation, and the brief ones get the dependency rule.
        # The verdict and the moderator propose nothing, so they do not.
        rule = "Do not install packages or add dependencies."
        self.assertTrue(all(rule in request["system"] for request in server.requests
                            if not request["system"].startswith(("Compare the exact claim", "Compare each numbered fact",
                                                                   MODERATOR_PROMPT))))
        self.assertEqual(brief["temperature"], 0.3)
        summary = brief["messages"][1]["content"]
        self.assertIn("Task: Fix it.", summary)
        self.assertIn("- read_file missing.txt", summary)
        self.assertIn("status.txt", summary)

        results = next(node.message["content"] for node in engine.tree.nodes
                       if str(node.message.get("content", "")).startswith("Reflection results"))
        proposals = next(event for event in events if event.startswith("Strategy\nReflection proposals"))
        self.assertIn("Proven ideas\n- Diagnosis: Check whether missing.txt exists. Facts: status.txt line 1 says "
                      "old_ready. Basis: it fits. Cost: one command. Fails if: nothing changes.\n- Observe: Print the "
                      "directory listing.", proposals)
        self.assertIn("\n- Switch tools: Edit the bytes with Python.", proposals)
        self.assertNotIn("Unverified", results)

        explore = [request for request in server.requests if request["system"].startswith(REFLECTION_PROMPT)]
        self.assertIn("Do not assume anything that no tool result", explore[0]["system"])
        self.assertEqual([request["tool_choice"] for request in explore], ["required"] * 3)
        # Each idea's facts went to a verifier before the main branch saw it.
        self.assertEqual(sum(event.startswith("Verifying These facts hold in the workspace") for event in events), 3)
        # The moderator, not the stalled main branch, chooses the idea, from the proven ideas alone.
        choice = next(request for request in server.requests if request["response_format"]
                      and "role" in request["response_format"]["schema"]["properties"])
        self.assertTrue(choice["system"].startswith(MODERATOR_PROMPT))
        self.assertEqual((choice["tools"], len(choice["messages"])), ([], 2))
        self.assertEqual(list(choice["response_format"]["schema"]["properties"]), ["reason", "first_step", "role"])
        self.assertEqual(choice["response_format"]["schema"]["properties"]["role"]["enum"],
                         ["Diagnosis", "Observe", "Switch tools", "none"])
        chosen = next(node.message["content"] for node in engine.tree.nodes
                      if str(node.message.get("content", "")).startswith("Reflection results"))
        self.assertIn("Chosen idea\n- Observe: Print the directory listing.", chosen)
        self.assertNotIn("Check whether missing.txt exists", chosen)
        self.assertIn("The moderator chose the Observe idea. Reason: a.txt line 1 explains the stall First step: "
                      "Run the check.", chosen)
        self.assertTrue(chosen.endswith("Carry out the moderator's plan now, starting with its first step."))
        summary = next(event for event in events if event.startswith("Reflection: "))
        self.assertRegex(summary, r"^Reflection: 3 of 3 ideas proven; the moderator chose Observe: Run the check\. "
                                  r"\(\d+ s\)$")

    def test_final_answer_while_the_check_fails_starts_reflection(self):
        check = "echo FAILED; exit 1"
        replies = [call("bash", command=check), call("write_file", path="a.txt", content="x"),
                   call("bash", command=check), text("The environment must be broken."), text("Done.")]
        engine, server, events = make_engine(self.root, replies, verification=False, route=reflection_route())
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

    def test_only_ideas_with_proven_facts_reach_the_main_branch(self):
        route = reflection_route(facts={"Diagnosis": "status.txt line 1 says old_ready",
                                        "Observe": "missing.txt line 1 says ready", "Switch tools": ""},
                                 statuses={"missing.txt": "contradicted"})
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt"),
                   text("Done.")]
        engine, server, events = make_engine(self.root, replies, route=route)
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "Done.")
        results = next(node.message["content"] for node in engine.tree.nodes
                       if str(node.message.get("content", "")).startswith("Reflection results"))
        self.assertIn("Chosen idea\n- Diagnosis: Diagnosis idea.", results)
        self.assertNotIn("Observe idea", results)
        self.assertNotIn("Switch tools idea", results)
        self.assertIn("Reflection Observe: dropped, its fact is not confirmed. contradicted by the workspace", events)
        self.assertIn("Reflection Switch tools: dropped, the idea names no facts.", events)
        choice = next(request for request in server.requests if request["response_format"]
                      and "role" in request["response_format"]["schema"]["properties"])
        self.assertEqual(choice["response_format"]["schema"]["properties"]["role"]["enum"], ["Diagnosis", "none"])

    def test_the_bookworm_reviewer_gets_the_project_documents(self):
        (Path(self.root) / "AGENTS.md").write_text("Never retry missing.txt.\n", encoding="utf-8")
        route = reflection_route(facts="AGENTS.md line 1 says never retry missing.txt")
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt"), text("Done.")]
        engine, server, events = make_engine(self.root, replies, route=route)
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "Done.")
        self.assertIn("Temperature 0: Bookworm, Diagnosis, Switch tools", next(e for e in events if "Reflecting" in e))
        request = next(message["content"] for request in server.requests for message in request["messages"]
                       if "Reflection role: Bookworm" in str(message.get("content")))
        self.assertIn("Project documents\nAGENTS.md", request)

    def test_each_fact_of_an_idea_is_judged_on_its_own(self):
        # The real case: an idea that named the real bug was rejected, because one of its facts gave a wrong line.
        route = reflection_route(facts={"Diagnosis": ["status.txt line 1 says old_ready", "wrong line 5 pushes"],
                                        "Observe": ["wrong one", "unclear two"], "Switch tools": ""},
                                 statuses={"wrong": "contradicted", "unclear": "insufficient"})
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt"), text("Done.")]
        engine, server, events = make_engine(self.root, replies, route=route)
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "Done.")
        results = next(node.message["content"] for node in engine.tree.nodes
                       if str(node.message.get("content", "")).startswith("Reflection results"))
        self.assertIn("- Diagnosis: Diagnosis idea. Facts: status.txt line 1 says old_ready. Basis: it fits.", results)
        self.assertIn("(1 of 2 facts confirmed.) Refuted: wrong line 5 pushes", results)
        self.assertIn("Reflection Observe: dropped, none of its 2 facts is confirmed. contradicted by the workspace",
                      events)
        verifier = next(request for request in server.requests
                        if request["system"].startswith("Compare each numbered fact"))
        self.assertEqual(verifier["response_format"]["schema"]["properties"]["verdicts"]["maxItems"], 2)
        reviewer = next(request for request in server.requests if request["response_format"]
                        and "proposal" in request["response_format"]["schema"]["properties"])
        self.assertEqual(reviewer["response_format"]["schema"]["properties"]["facts"]["maxItems"], 3)
        self.assertIn("Be short and concise", reviewer["system"])

    def test_without_a_proven_idea_there_is_no_decision(self):
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt"), text("Done.")]
        engine, server, events = make_engine(self.root, replies, route=reflection_route(status="insufficient"))
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "Done.")
        results = next(node.message["content"] for node in engine.tree.nodes
                       if str(node.message.get("content", "")).startswith("Reflection results"))
        self.assertIn("No reviewer idea rested on facts that a verifier could confirm.", results)
        # The note must not claim confirmed facts that no idea had.
        self.assertNotIn("A verifier confirmed", results)
        self.assertIn("Take the observed stall itself as the finding.", results)
        self.assertFalse(any(request["response_format"] and "role" in request["response_format"]["schema"]["properties"]
                             for request in server.requests))
        self.assertEqual(sum("its fact is not confirmed" in event for event in events), 3)

    def test_an_unusable_choice_still_shows_the_proven_ideas(self):
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt"), text("Done.")]
        engine, _, events = make_engine(self.root, replies, route=reflection_route(choose="invalid"))
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "Done.")
        results = next(node.message["content"] for node in engine.tree.nodes
                       if str(node.message.get("content", "")).startswith("Reflection results"))
        self.assertIn("Proven ideas\n- Diagnosis: Diagnosis idea.", results)
        self.assertTrue(any(event.startswith("Moderator\nNo answer:") for event in events))

    def test_the_moderator_may_choose_no_idea(self):
        replies = [call("read_file", path="missing.txt"), call("read_file", path="missing.txt"), text("Done.")]
        engine, _, events = make_engine(self.root, replies, route=reflection_route(choose="none"))
        self.assertEqual(asyncio.run(engine.respond("Fix it.")), "Done.")
        results = next(node.message["content"] for node in engine.tree.nodes
                       if str(node.message.get("content", "")).startswith("Reflection results"))
        self.assertIn("The moderator found that no proven idea serves the user's request.", results)
        self.assertNotIn("Diagnosis idea", results)

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
        # The test engine's replies may take 256 tokens, so the conversation has 744 of the 1,000.
        engine, _, events = make_engine(self.root, [*reads, text("Done.")], forks=False,
                                        tokens=[500, 560, 600, 700], context_size=1000)
        self.assertEqual(asyncio.run(engine.respond("Read the status.")), "Done.")
        warnings = [event for event in events if event.startswith("Context:")]
        self.assertEqual([warning.split(". ")[0] for warning in warnings],
                         ["Context: 560 of 744 tokens in use (75%), keeping 256 of the 1,000 for a reply",
                          "Context: 700 of 744 tokens in use (94%), keeping 256 of the 1,000 for a reply"])
        self.assertIn("When it is full, Reflect gives replies less room and then shortens older messages.",
                      warnings[0])

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

    def test_a_reply_gets_less_room_when_the_server_counts_it(self):
        # Basalt counts max_tokens against the context. The room left is enough while it is a quarter of the limit.
        replies = [ContextLimitError(50000, 65536, reply_tokens=20000), text("Done.")]
        engine, server, events = make_engine(self.root, replies, forks=False, max_tokens=20000, thinking_budget=10000)
        self.assertEqual(asyncio.run(engine.respond("hi")), "Done.")
        self.assertEqual([request["max_tokens"] for request in server.requests], [20000, 15520])
        self.assertEqual([request["thinking_budget"] for request in server.requests], [10000, 7760])
        self.assertFalse(any(event.startswith("Context: shortened") for event in events))

    def test_older_messages_are_shortened_when_the_conversation_does_not_fit(self):
        (Path(self.root) / "big.txt").write_text("x" * 3000 + "\n", encoding="utf-8")
        reads = [call("read_file", path="big.txt") for _ in range(7)]
        engine, server, events = make_engine(self.root, [*reads, ContextLimitError(1200, 1000), text("Done."),
                                                         text("Again.")], forks=False)
        self.assertEqual(asyncio.run(engine.respond("Read it.")), "Done.")
        self.assertIn("Context: shortened the 3 older messages to fit the server's context: long tool output and "
                      "file contents are cut, and images are left out.", events)
        retried = server.requests[-1]["messages"]
        self.assertIn("characters left out to save context", retried[3]["content"])
        self.assertNotIn("characters left out", retried[-1]["content"])
        # Later requests keep the shortening, so the server's cache of the conversation holds.
        self.assertEqual(asyncio.run(engine.respond("Once more.")), "Again.")
        self.assertIn("characters left out to save context", server.requests[-1]["messages"][3]["content"])

    def test_only_the_latest_thinking_goes_back_to_the_server(self):
        first = {**call("read_file", path="status.txt"), "reasoning_content": "first thoughts"}
        second = {**call("read_file", path="status.txt"), "reasoning_content": "second thoughts"}
        engine, server, _ = make_engine(self.root, [first, second, text("Done.")], forks=False)
        self.assertEqual(asyncio.run(engine.respond("Read it.")), "Done.")
        sent = [message.get("reasoning_content") for message in server.requests[-1]["messages"]
                if message["role"] == "assistant"]
        self.assertEqual(sent, [None, "second thoughts"])
        stored = [node.message.get("reasoning_content") for node in engine.tree.nodes
                  if node.message["role"] == "assistant" and node.message.get("tool_calls")]
        self.assertEqual(stored, ["first thoughts", "second thoughts"])


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

    def test_forks_do_not_get_the_main_branchs_images(self):
        (self.root / "square.png").write_bytes(png(8, 8))
        claim = "<verify>square.png is red</verify>"
        engine, server, _ = make_engine(self.root, [
            call("view_image", path="square.png"), text(f"It is red. {claim}"),
            call("read_file", path="status.txt"), verdict("supported", "status.txt line 1"), text("It is red."),
        ], vision=True)
        self.assertEqual(asyncio.run(engine.respond("What colour is square.png?")), "It is red.")

        def images(request):
            return sum(isinstance(message["content"], list) for message in request["messages"])

        main = [request for request in server.requests if request["system"].startswith("You are Reflect, a coding")]
        fork = [request for request in server.requests if request["system"].startswith("You are Reflect's read-only")]
        self.assertTrue(main and fork)
        self.assertTrue(all(images(request) == 1 for request in main[1:]))
        self.assertEqual(images(fork[0]), 0)
        self.assertTrue(any("The image went to the main branch." in str(message["content"])
                            for message in fork[0]["messages"]))
        # The session keeps the image.
        self.assertTrue(any('"image": "data:image/png' in str(node.message.get("content"))
                            for node in engine.tree.nodes))

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


def _critic_route(objections, verdicts=None, rulings=None, checks=None, explore=True, simpler=""):
    """Answer the forks of a plan challenge by their content, so their order does not matter.

    `objections` maps a critic to its objection, `verdicts` a word in a finding to the verifier's status, and
    `simpler` is the simplifier's strategy, empty for none. `rulings` maps a critic to the moderator's ruling,
    follow by default, and `checks` lists the moderator's checks of the revised plan in turn, complied by default.
    """
    checks = list(checks or [])

    def route(request):
        system, schema = request["system"], (request["response_format"] or {}).get("schema", {})
        properties = schema.get("properties", {})
        if system.startswith((CHALLENGE_PROMPT, SIMPLIFY_PROMPT)) and request["tools"]:
            return call("read_file", path="status.txt") if explore else text("Nothing to read.")
        if "simpler_plan" in properties:
            return text(json.dumps({"goal": "status.txt says new_ready", "simpler_plan": simpler,
                                    "relies_on": "status.txt", "why_simpler": "one edit instead of a script",
                                    "requirements": "keeps line 1"}))
        if "objection" in properties:
            critic = system.split("Your role: ")[1].split(".")[0]
            return text(json.dumps({"objection": objections.get(critic, ""), "evidence": "status.txt line 1",
                                    "check": "read status.txt", "likelihood": "high"}))
        if system.startswith("You are Reflect's read-only verifier"):
            return call("read_file", path="status.txt")
        if system.startswith(MODERATOR_PROMPT) and RULING_PROMPT in system:
            findings = request["messages"][1]["content"].split("confirmed\n", 1)[1].splitlines()
            answer = {}
            for line in findings:
                number, _, rest = line.partition(". ")
                critic = rest.split(":")[0]
                answer[number] = {"reason": f"status.txt line 1 bears on {critic}",
                                  "ruling": (rulings or {}).get(critic, "follow")}
            return text(json.dumps(answer))
        if system.startswith(MODERATOR_PROMPT) and CHECK_PROMPT in system:
            status = checks.pop(0) if checks else "complied"
            return text(json.dumps({"reason": "The plan still writes old_ready.", "status": status}))
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

    def test_the_panel_hears_each_critic_the_moderator_and_the_main_branch(self):
        route = _critic_route({"Requirements": "It keeps old_ready"}, verdicts={"old_ready": "supported"})
        replies = [call("write_file", path="app.py", content=_LARGE), text("I will write new_ready."),
                   call("write_file", path="app.py", content=_LARGE), text("Done.")]
        reports = []
        engine, _, _ = make_engine(self.root, replies, challenge=True, route=route,
                                   on_progress=lambda **fields: reports.append(fields))
        self.assertEqual(asyncio.run(engine.respond("Write app.py.")), "Done.")
        actors = [report["actor"] for report in reports if "actor" in report]
        phases = [report["phase"] for report in reports if "phase" in report]
        # The first write waits for the plan check, so it runs only when the main branch reissues it.
        self.assertEqual(phases, ["Next step, thinking", "Plan check", "Next step, thinking",
                                  "Running write_file app.py", "Next step, thinking"])
        self.assertIn(("Security", "waiting for a slot", "wait"), actors)
        self.assertIn(("Requirements", "checking read_file status.txt", "work"), actors)
        self.assertIn(("Requirements", "objection proven", "good"), actors)
        self.assertIn(("Environment", "nothing found", "quiet"), actors)
        self.assertIn(("Moderator", "ruling", "work"), actors)
        self.assertIn(("Moderator", "1 follow", "good"), actors)
        self.assertIn(("Main branch", "revising its plan", "work"), actors)
        critics = [report["critics"] for report in reports if "critics" in report]
        self.assertEqual((critics[0], critics[-1]), ((0, 6), (6, 6)))
        # With a panel listening, every request streams, and each actor's tokens arrive as they come.
        streams = [report["stream"] for report in reports if "stream" in report]
        self.assertIn(("Requirements", "waiting", 0, 0.0, ""), streams)
        self.assertTrue(any(name == "Moderator" and kind == "writing" for name, kind, *_ in streams))
        self.assertTrue(any(name == "Main branch" and kind == "done" and tokens > 0
                            for name, kind, tokens, *_ in streams))
        self.assertTrue(all(request["streamed"] for request in engine.server.requests))

    def test_the_bookworm_critic_reads_the_project_documents(self):
        (Path(self.root) / "docs").mkdir()
        (Path(self.root) / "docs" / "speed.md").write_text("Dead end: a cache of app.py results.\n", encoding="utf-8")
        route = _critic_route({"Bookworm": "docs/speed.md line 1 records the cache as a dead end"},
                              verdicts={"dead end": "supported"})
        replies = [call("write_file", path="app.py", content=_LARGE), text("I will not add the cache."),
                   call("write_file", path="app.py", content=_LARGE), text("Done.")]
        engine, server, events = make_engine(self.root, replies, challenge=True, route=route)
        self.assertEqual(asyncio.run(engine.respond("Write app.py.")), "Done.")
        request = next(request for request in server.requests if "Critic role: Bookworm" in str(request["messages"]))
        brief = next(message["content"] for message in request["messages"]
                     if "Critic role: Bookworm" in str(message.get("content")))
        self.assertIn("Project documents\ndocs/speed.md", brief)
        self.assertTrue(any("Bookworm" in event and "dead end" in event for event in events))

    def test_without_documents_there_is_no_bookworm_critic(self):
        route = _critic_route({})
        replies = [call("write_file", path="app.py", content=_LARGE), text("Done.")]
        engine, server, _ = make_engine(self.root, replies, challenge=True, route=route)
        asyncio.run(engine.respond("Write app.py."))
        self.assertFalse(any("Critic role: Bookworm" in str(request["messages"]) for request in server.requests))

    def test_the_moderator_rules_on_proven_findings_and_checks_the_revised_plan(self):
        route = _critic_route({"Requirements": "It keeps old_ready", "Environment": "Python is missing"},
                              verdicts={"old_ready": "supported", "Python": "contradicted"},
                              checks=["not_complied", "complied"])
        replies = [call("write_file", path="app.py", content=_LARGE),
                   text("I will write new_ready."),
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
        self.assertIn("Follow these findings\n- Requirements: (high) It keeps old_ready. Evidence: status.txt line 1. "
                      "Check: read status.txt. Verifier: supported by status.txt line 1 Moderator: status.txt line "
                      "1 bears on Requirements", challenge)
        self.assertNotIn("Python is missing", challenge)
        self.assertTrue(any("Dropped the Environment objection" in event for event in events))
        retry = next(content for content in contents if content.startswith("Not followed yet"))
        self.assertIn("does not follow its rulings yet: The plan still writes old_ready.", retry)
        self.assertEqual(contents.count("Carry out the revised plan now. Reissue the deferred calls that are still "
                                        "needed."), 1)
        summary = next(event for event in events if event.startswith("Plan check: "))
        self.assertRegex(summary, r"^Plan check: 1 proven, 1 to follow, 0 dismissed; the revised plan follows them "
                                  r"\(\d+ s\)$")

        # The moderator sees only the request, the planned change, and the findings, not the conversation.
        moderator = [request for request in server.requests if request["system"].startswith(MODERATOR_PROMPT)]
        self.assertEqual(len(moderator), 3)
        self.assertTrue(all(len(request["messages"]) == 2 and request["tools"] == [] for request in moderator))
        self.assertTrue(moderator[0]["messages"][1]["content"].startswith(
            "User's request: Write app.py.\n\nPlanned change: write_file app.py\n\nFindings"))
        self.assertIn("The main branch's revised plan\nI will write new_ready.", moderator[1]["messages"][1]["content"])
        # Each critic, verifier, and moderator question has its own session: six critics and the simplifier
        # minus four without an objection, the verifiers of two objections, and three moderator questions.
        stem = engine.tree.path.stem
        forks = {request["session"] for request in server.requests if request["session"] != stem}
        self.assertEqual(len(forks), 6 + 2 + 3)
        # Every critic sees the user's request in its task.
        self.assertTrue(all("User's request: Write app.py." in request["messages"][-1]["content"]
                            for request in server.requests if request["system"].startswith(CHALLENGE_PROMPT)
                            and request["tools"]))
        # A saved session with challenge and moderator branches loads again.
        resumed = ConversationTree(engine.tree.path.parent, "system", resume=stem)
        self.assertEqual(len(resumed.nodes), len(engine.tree.nodes))

    def test_a_dismissed_finding_never_reaches_the_main_branch(self):
        route = _critic_route({"Quality": "It duplicates read_status"}, verdicts={"duplicates": "supported"},
                              rulings={"Quality": "dismiss"})
        replies = [call("write_file", path="a.txt", content=_LARGE),
                   call("write_file", path="a.txt", content=_LARGE), text("Done.")]
        engine, _, events = make_engine(self.root, replies, challenge=True, route=route)
        self.assertEqual(asyncio.run(engine.respond("Write a.")), "Done.")
        contents = [str(message.get("content")) for message in engine.tree.messages(engine.tree.head)]
        self.assertFalse(any("duplicates read_status" in content for content in contents))
        self.assertIn("Moderator\nNo proven finding calls for a change of plan.", events)
        self.assertTrue(any(event.startswith("Moderator\ndismiss: Quality:") for event in events))

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

    def test_a_proven_simpler_strategy_the_moderator_follows_must_be_taken(self):
        route = _critic_route({}, verdicts={"Edit line 1": "supported"}, simpler="Edit line 1 of status.txt",
                              checks=["not_complied", "complied"])
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
        self.assertIn("Follow these findings\n- Simpler plan: Goal: status.txt says new_ready. Simpler strategy: "
                      "Edit line 1 of status.txt. Relies on: status.txt. Saves: one edit instead of a script. Keeps: "
                      "keeps line 1. Verifier: supported by status.txt line 1", challenge)
        # The verifier checks what the strategy rests on today, not a prediction that it will work.
        claim = next(event for event in events if event.startswith("Verifying The workspace has"))
        self.assertEqual(claim, "Verifying The workspace has what this strategy relies on, and it does what the "
                                "strategy needs: status.txt. The request and any spec or tests in the workspace set "
                                "no requirement beyond these: keeps line 1. Strategy: Edit line 1 of status.txt")
        self.assertTrue(any(content.startswith("Not followed yet") for content in contents))
        # The simplifier states the goal before its strategy.
        schema = next(request["response_format"]["schema"] for request in server.requests
                      if request["response_format"] and "simpler_plan" in request["response_format"]["schema"]["properties"])
        self.assertEqual(schema["required"][:2], ["goal", "simpler_plan"])

    def test_the_user_is_asked_when_the_moderator_rules_so(self):
        route = _critic_route({"Requirements": "It keeps old_ready"}, verdicts={"old_ready": "supported"},
                              rulings={"Requirements": "ask_user"})
        question = ("I was about to write app.py, but status.txt still says old_ready. Should app.py replace it, "
                    "or keep it?")
        replies = [call("write_file", path="app.py", content=_LARGE), text(question)]
        engine, server, events = make_engine(self.root, replies, challenge=True, route=route)
        self.assertEqual(asyncio.run(engine.respond("Write app.py.")), question)
        # The deferred change stays unmade, and the turn ends with the question.
        self.assertFalse((self.root / "app.py").exists())
        self.assertFalse(server.replies)
        self.assertTrue(any(event.startswith("Moderator\nOnly you can settle this") for event in events))
        ask = server.requests[-1]
        self.assertEqual(ask["tools"], [])
        self.assertTrue(ask["messages"][-1]["content"].startswith("For the user"))
        self.assertIn("ask one or two precise questions about how the user wants the request specified",
                      ask["messages"][-1]["content"])
        self.assertEqual(engine.tree.nodes[engine.tree.head].message["content"], question)

    def test_the_user_is_asked_when_the_plan_twice_fails_the_rulings(self):
        route = _critic_route({"Requirements": "It keeps old_ready"}, verdicts={"old_ready": "supported"},
                              checks=["not_complied", "not_complied"])
        replies = [call("write_file", path="app.py", content=_LARGE),
                   text("It does not apply."), text("It still does not apply."), text("How should I proceed?")]
        engine, server, events = make_engine(self.root, replies, challenge=True, route=route)
        self.assertEqual(asyncio.run(engine.respond("Write app.py.")), "How should I proceed?")
        self.assertFalse((self.root / "app.py").exists())
        self.assertFalse(server.replies)
        self.assertIn("the revised plan still does not follow these", server.requests[-1]["messages"][-1]["content"])

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

    def test_scratch_files_in_hidden_folders_are_not_challenged(self):
        route = _critic_route({}, explore=False)
        replies = [call("write_file", path=".tmpcheck/harness.js", content=_LARGE),
                   call("write_file", path=".github/workflows/ci.yml", content=_LARGE),
                   call("write_file", path=".github/workflows/ci.yml", content=_LARGE), text("Done.")]
        engine, _, events = make_engine(self.root, replies, challenge=True, route=route)
        self.assertEqual(asyncio.run(engine.respond("Add a CI workflow.")), "Done.")
        started = [event for event in events if event.startswith("Challenge\nLooking for reasons")]
        self.assertEqual(len(started), 1)
        self.assertIn("write_file .github/workflows/ci.yml", started[0])

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
                              explore=False)
        # Five requests give the first critic its answer and its verifier one step. The third critic and the
        # verdict find the budget used up, so the objection stays unproven and is dropped.
        replies = [call("write_file", path="a.txt", content=_LARGE),
                   call("write_file", path="a.txt", content="a\n"), text("Done.")]
        engine, _, events = make_engine(self.root, replies, challenge=True, route=route)
        with mock.patch("refl.engine.CHALLENGE_REQUESTS", 5):
            self.assertEqual(asyncio.run(engine.respond("Write a.")), "Done.")
        self.assertIn("Challenge Edge cases: The plan challenge used all of its requests.", events)
        challenge = next(node.message["content"] for node in engine.tree.nodes
                         if str(node.message.get("content")).startswith("Plan challenge"))
        self.assertIn("Challenge\nDropped the Requirements objection, not proven: The plan challenge used all of "
                      "its requests.", events)
        self.assertIn("No objection to the plan held up", challenge)
        self.assertIsNone(engine.batch_left)
        self.assertTrue((self.root / "a.txt").exists())

