import asyncio
import json
import re

from .client import ServerError
from .reflection import LENSES, REFLECTION_PROMPT, StallDetector
from .tools import TOOLS
from .tree import ensure_git_ignored


_PROMPT_START = (
    "You are Reflect, a coding agent working in the current workspace. "
    "Use the provided tools to carry out the user's request. Use workspace-relative paths. "
    "Read a file before editing it and check the result of each tool call. "
    "If a tool returns an error or a nonzero exit code, use that information to recover. "
)
_VERIFICATION_INSTRUCTION = (
    "On the main branch, before acting on an unchecked assumption about a file, API, or config key, "
    "emit <verify>specific claim</verify> and wait for the verification verdict. "
)
_PROMPT_END = (
    "Prefer existing tools for mechanical work and check the project skills for a relevant workflow. "
    "On the main branch, after confirming a reusable command or workflow, write or update a short Markdown "
    "skill under .refl/skills/ with when to use it and the command that worked. "
    "When the task is complete, give a brief final answer."
)
SYSTEM_PROMPT = _PROMPT_START + _VERIFICATION_INSTRUCTION + _PROMPT_END
_LINEAR_PROMPT = _PROMPT_START + _PROMPT_END

_VERIFY_TAG = re.compile(r"<verify>(.*?)</verify>", re.DOTALL | re.IGNORECASE)
_READ_ONLY_NAMES = {"read_file", "grep_search"}
_READ_ONLY_TOOLS = [tool for tool in TOOLS if tool["function"]["name"] in _READ_ONLY_NAMES]
_BASH_TOOLS = [tool for tool in TOOLS if tool["function"]["name"] == "bash"]
_REVIEW_REQUESTS = 2
_PROJECT_WRITE_RULE = (
    "Preserve existing AGENTS.md, AGENTS.override.md, CLAUDE.md, CLAUDE.local.md, .mcp.json, and configuration "
    "under .agents/, .claude/, .codex/, and .pi/ when using tools, including Bash. Project skill files can be "
    "updated when relevant."
)
_VERDICT_LABELS = {"supported": "VERIFIED", "contradicted": "FALSE ASSUMPTION", "insufficient": "UNVERIFIED"}
_VERDICT_SCHEMA = {
    "type": "json_object",
    "schema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "status": {"type": "string", "enum": list(_VERDICT_LABELS)},
        },
        "required": ["reason", "status"],
        "additionalProperties": False,
    },
}
_VERIFY_PROMPT = (
    "You are Reflect's read-only verifier. Earlier messages provide context. Check only the latest claim. "
    "Start with a tool call to read_file for a named file or grep_search for a narrow search. "
    "Your tool results will be used to decide the verdict."
)
_EVIDENCE_PROMPT = (
    "Compare the exact claim with the supplied workspace evidence. In reason, explain what the source shows, "
    "say whether it agrees with the claim, and cite the file and line if available. Set status to supported "
    "if the evidence confirms the claim, contradicted if it disproves the claim, or insufficient if it does neither."
)
_RECOMMENDATION_PROMPT = (
    "You are a read-only reviewer summarizing why a coding task stalled. In recommendation, give one "
    "concrete next action, the evidence behind it, and a check the main branch can run. Keep it brief."
)
_RECOMMENDATION_SCHEMA = {
    "type": "json_object",
    "schema": {
        "type": "object",
        "properties": {"recommendation": {"type": "string"}},
        "required": ["recommendation"],
        "additionalProperties": False,
    },
}


class TurnLimitError(RuntimeError):
    """The turn used all of its allotted model requests."""


def _call_summary(function):
    """Describe a tool call for the terminal and the strategy log, such as `bash pytest -x`."""
    name = function["name"]
    try:
        args = json.loads(function["arguments"])
    except (TypeError, ValueError):
        args = None
    parts = [name]
    if isinstance(args, dict):
        if name == "grep_search":
            parts += [json.dumps(args.get("pattern", ""), ensure_ascii=False), str(args.get("path", ""))]
        else:
            parts.append(str(args.get("command") or args.get("path") or ""))
    text = " ".join(" ".join(parts).split())
    return text if len(text) <= 200 else text[:197] + "..."


def _tool_calls(message):
    calls = message.get("tool_calls")
    if calls is None:
        return []
    if not isinstance(calls, list):
        raise ServerError("Invalid tool_calls in the assistant message")
    ids = set()
    for call in calls:
        try:
            call_id = call["id"]
            function = call["function"]
            valid = (call["type"] == "function" and isinstance(call_id, str) and call_id
                     and isinstance(function["name"], str) and isinstance(function["arguments"], str))
        except (KeyError, TypeError) as error:
            raise ServerError("Invalid function call in the assistant message") from error
        if not valid or call_id in ids:
            raise ServerError("Invalid or duplicate function call in the assistant message")
        ids.add(call_id)
    return calls


class Engine:
    def __init__(self, server, model, workspace, tree, max_turns, max_tokens, on_tool, *, forks=True):
        self.server = server
        self.model = model
        self.workspace = workspace
        self.tree = tree
        self.stalls = StallDetector(tree.reflection)
        self.max_turns = max_turns
        self.requests_left = None
        self.max_tokens = max_tokens
        self.on_tool = on_tool
        self.forks = forks
        self.strategy_sections = []
        self.track_strategy = False
        self.strategy_path = f".refl/strategies/{tree.path.stem}.md"
        # Verdicts from the current task, reused until a main-branch command or write can change the evidence.
        self.verdicts = {}

    def _messages(self, leaf, *, system_prompt=None):
        messages = self.tree.messages(leaf)
        skills = self.workspace.skill_paths()
        # Build the main prompt for each request so resumed sessions and --linear use the current instructions.
        system = system_prompt or (SYSTEM_PROMPT if self.forks else _LINEAR_PROMPT)
        system += "\n\n" + _PROJECT_WRITE_RULE
        guidance = self.workspace.project_guidance()
        if guidance:
            system += "\n\nProject guidance from REFLECT.md:\n" + guidance
        if skills:
            system += "\n\nRead relevant project skills before investigating: " + json.dumps(skills, ensure_ascii=False)
        messages[0] = {**messages[0], "content": system}
        return messages

    async def _complete(self, messages, tools, *, fork=False, response_format=None):
        if self.requests_left is not None:
            if self.requests_left <= (1 if fork else 0):
                raise TurnLimitError("No model requests remain for this step.")
            self.requests_left -= 1
        return await self.server.complete(self.model, messages, tools, self.max_tokens, response_format=response_format)

    def _strategy_event(self, text):
        self.strategy_sections.append(text)
        try:
            ensure_git_ignored(self.workspace.root / ".refl" / "strategies")
            self.workspace.write_file(self.strategy_path, "# Strategy\n\n" + "\n\n".join(self.strategy_sections) + "\n")
        except (OSError, RuntimeError, ValueError) as error:
            self.on_tool(f"Strategy log unavailable: {error}")

    async def _observe(self, leaf, calls, *, branch_type="main", defer=False, stalls=None):
        read_only = branch_type != "main"
        prefix = {"verification": "verify ", "reflection": "reflect "}.get(branch_type, "")
        evidence = []
        for call in calls:
            function = call["function"]
            name = function["name"]
            if defer:
                result = {"error": "Tool call deferred. Reissue it after reviewing the verification verdict."}
            elif read_only and name not in _READ_ONLY_NAMES:
                result = {"error": f"{name} is unavailable in a read-only {branch_type} fork."}
            else:
                summary = _call_summary(function)
                tracking = not read_only and self.track_strategy
                if tracking:
                    self._strategy_event(f"### Attempt\n\n    {summary}")
                    self.on_tool(f"Strategy\nTrying {summary}")
                else:
                    self.on_tool(prefix + summary)
                result = await asyncio.to_thread(self.workspace.execute, function)
                if not read_only and (name == "bash" or result.get("changed") is True):
                    self.verdicts.clear()
                if tracking:
                    observation = json.dumps(result, ensure_ascii=False) if "output" in result or "error" in result else str(result)
                    self._strategy_event(f"Result\n\n    {observation[:600]}")
                if "output" in result and (name == "read_file" or result.get("changed") is True):
                    skill = self.workspace.skill_path(json.loads(function["arguments"])["path"])
                    if skill:
                        self.on_tool(f"Skills\n{'Read' if name == 'read_file' else 'Saved'} {skill}")
                if stalls is not None:
                    stalls.record(function, result)
                if branch_type == "verification" and "output" in result and "error" not in result:
                    evidence.append(f"{name} {function['arguments']}\n{result['output']}")
            leaf = self.tree.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result, ensure_ascii=False),
            }, parent_id=leaf, branch_type=branch_type)
        return leaf, evidence

    async def _verify(self, anchor, claim):
        self.on_tool(f"Verifying {claim}")
        leaf = self.tree.append({
            "role": "user",
            "content": (
                "Use read_file or grep_search to check this claim. Call the tool now so you can see its output.\n"
                f"Claim to check: {claim}"
            ),
        }, parent_id=anchor, branch_type="verification")
        reason = "Verification reached its three-request limit."
        try:
            for attempt in range(3):
                message = await self._complete(
                    self._messages(leaf, system_prompt=_VERIFY_PROMPT), _READ_ONLY_TOOLS, fork=True,
                )
                calls = _tool_calls(message)
                leaf = self.tree.append(message, parent_id=leaf, branch_type="verification")
                if not calls:
                    if attempt < 2:
                        leaf = self.tree.append({
                            "role": "user",
                            "content": "Call read_file or grep_search to get workspace evidence before giving a verdict.",
                        }, parent_id=leaf, branch_type="verification")
                    continue

                leaf, evidence = await self._observe(leaf, calls, branch_type="verification")
                if not evidence or attempt == 2:
                    continue
                messages = [
                    {"role": "system", "content": _EVIDENCE_PROMPT},
                    {"role": "user", "content": f"Claim: {claim}\n\nCurrent evidence:\n" + "\n\n".join(evidence)},
                ]
                message = await self._complete(
                    messages, [], fork=True, response_format=_VERDICT_SCHEMA,
                )
                leaf = self.tree.append(message, parent_id=leaf, branch_type="verification")
                try:
                    verdict = json.loads(message.get("content") or "")
                    label = _VERDICT_LABELS[verdict["status"]]
                    detail = " ".join(verdict["reason"].split())
                    if detail:
                        self.tree.finish_fork(leaf, anchor, "merged")
                        return f"[{label}] {detail[:500]}"
                except (KeyError, AttributeError, TypeError, ValueError):
                    pass
                reason = "The verifier returned an invalid verdict."
                break
        except (ServerError, TurnLimitError) as error:
            reason = str(error)

        self.tree.finish_fork(leaf, anchor, "pruned")
        return f"[UNVERIFIED] {reason}"

    async def _review(self, anchor, stall, name, focus):
        self.on_tool(f"Reviewing {name.lower()}: {focus}")
        history = ""
        skill = None
        if name == "Existing patterns":
            history = await asyncio.to_thread(self.workspace.recent_commits)
            paths = self.workspace.skill_paths()
            user_messages = (message.get("content", "") for message in reversed(self.tree.messages(anchor))
                             if message["role"] == "user")
            skill = next((path for content in user_messages if isinstance(content, str) for path in paths
                          if path in content), None)
            if skill is None and len(paths) == 1:
                skill = paths[0]
        request = f"Reflection lens: {name}\nReview reason: {stall}\n\n{focus}"
        if history:
            request += "\n\nRecent project commits\n" + history
        leaf = self.tree.append({
            "role": "user",
            "content": request,
        }, parent_id=anchor, branch_type="reflection")
        if skill:
            call = {
                "type": "function",
                "id": f"s{leaf:08x}",
                "function": {"name": "read_file", "arguments": json.dumps({"path": skill})},
            }
            leaf = self.tree.append({"role": "assistant", "content": None, "tool_calls": [call]},
                                    parent_id=leaf, branch_type="reflection")
            leaf, _ = await self._observe(leaf, [call], branch_type="reflection")
        reason = "The reviewer returned no proposal."
        try:
            for attempt in range(_REVIEW_REQUESTS):
                final = attempt == _REVIEW_REQUESTS - 1
                if final:
                    leaf = self.tree.append({
                        "role": "user",
                        "content": "Give your recommendation now using the evidence already in the conversation.",
                    }, parent_id=leaf, branch_type="reflection")
                prompt = _RECOMMENDATION_PROMPT if final else REFLECTION_PROMPT
                message = await self._complete(
                    self._messages(leaf, system_prompt=prompt), [] if final else _READ_ONLY_TOOLS, fork=True,
                    response_format=_RECOMMENDATION_SCHEMA if final else None,
                )
                calls = _tool_calls(message)
                if final and calls:
                    raise ServerError("The reviewer called a tool on its final request.")
                leaf = self.tree.append(message, parent_id=leaf, branch_type="reflection")
                if calls:
                    leaf, _ = await self._observe(leaf, calls, branch_type="reflection")
                    continue
                proposal = message.get("content")
                if final:
                    try:
                        proposal = json.loads(proposal)["recommendation"]
                    except (KeyError, TypeError, ValueError):
                        proposal = None
                if isinstance(proposal, str) and proposal.strip():
                    self.tree.finish_fork(leaf, anchor, "merged")
                    return " ".join(proposal.split())[:600]
                break
        except (ServerError, TurnLimitError) as error:
            reason = str(error)

        self.tree.finish_fork(leaf, anchor, "pruned")
        self.on_tool(f"Reflection {name}: {reason}")
        return None

    async def _reflect(self, reason, *, manual=False):
        anchor = self.tree.head
        heading = "Request" if manual else "Observed stall"
        self._strategy_event(f"## Reflection\n\n{heading}\n\n    {' '.join(reason.split())}")
        self.on_tool(f"Strategy\nReflecting: {reason}\nTracking: {self.strategy_path}")
        proposals = await asyncio.gather(*(self._review(anchor, reason, name, focus) for name, focus in LENSES))
        findings = [f"{name}: {proposal}" for (name, _), proposal in zip(LENSES, proposals) if proposal]
        summary = "\n".join(findings) or "The reviewers returned no usable proposals."
        self.on_tool(f"Strategy\nReflection proposals\n{summary}")
        self._strategy_event("### Proposals\n\n" + "\n".join(f"- {entry}" for entry in findings or [summary]))
        self.track_strategy = True
        self.tree.append({
            "role": "user",
            "content": (
                f"Reflection results\n{heading}: {reason}\n\n"
                + summary
                + "\n\nTry the smallest evidence-backed fix first. Start with the Simpler approach if it fits the evidence. "
                "For a localized fix, read only the lines you need and apply a targeted edit_file call. "
                "Preserve unrelated code. Focus on making the fix now and keep the final request for rerunning "
                "the smoke check that reproduced the failure."
            ),
        })

    def _close_unfinished_round(self):
        messages = self.tree.messages(self.tree.head)
        index = len(messages) - 1
        observed = set()
        while messages[index]["role"] == "tool":
            observed.add(messages[index].get("tool_call_id"))
            index -= 1
        if messages[index]["role"] == "assistant":
            for call in _tool_calls(messages[index]):
                if call["id"] not in observed:
                    self.tree.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps({"error": "The conversation was resumed before this result. Reissue the call if needed."}),
                    })
        if self.tree.nodes[self.tree.head].message["role"] in {"user", "tool"}:
            self.tree.append({"role": "assistant", "content": "[The previous turn ended before a final answer.]"})

    async def reflect(self):
        if not self.forks:
            raise ValueError("Run refl without --linear to use /reflect.")
        if self.tree.head == 0:
            raise ValueError("Start a task before using /reflect.")
        self._close_unfinished_round()
        self.tree.save()
        await self._reflect("Review the current conversation and propose the next action.", manual=True)
        self.tree.append({"role": "assistant", "content": "Reflection complete. The findings are ready for the next task."})
        self.tree.save()

    async def respond(self, prompt):
        self.stalls.start_turn()
        self.verdicts = {}
        self._close_unfinished_round()
        self.tree.append({"role": "user", "content": prompt})
        self.tree.save()
        self.requests_left = self.max_turns
        try:
            return await self._respond()
        except (ServerError, TurnLimitError) as error:
            self.tree.append({"role": "assistant", "content": f"[Turn stopped] {error}"})
            self.tree.save()
            raise
        finally:
            self.track_strategy = False
            self.requests_left = None

    async def _smoke_check(self):
        self.tree.append({
            "role": "user",
            "content": (
                "Run the smoke check now. Use one bash call with the command that reproduced the failure "
                "or the applicable check from the reflection findings. This is the final request. "
                "If there is no applicable smoke check, say so."
            ),
        })
        message = await self._complete(self._messages(self.tree.head), _BASH_TOOLS)
        calls = _tool_calls(message)
        if calls and (len(calls) != 1 or calls[0]["function"]["name"] != "bash"):
            raise ServerError("Expected one Bash smoke check on the final request.")
        leaf = self.tree.append(message)
        if not calls:
            self.tree.save()
            answer = message.get("content")
            if isinstance(answer, str) and answer.strip():
                return answer.strip()
            return "No smoke check was run."
        leaf, _ = await self._observe(leaf, calls, stalls=self.stalls)
        result = json.loads(self.tree.nodes[leaf].message["content"])
        if "error" in result:
            summary = f"Smoke check could not run. {result['error']}"
        else:
            summary = f"Smoke check exited with code {result['exit_code']}.\n{result['output']}".rstrip()
        self.tree.append({"role": "assistant", "content": summary})
        self.tree.save()
        return summary

    async def _respond(self):
        reflected = False
        while self.requests_left:
            if reflected and self.requests_left == 1:
                return await self._smoke_check()
            message = await self._complete(self._messages(self.tree.head), TOOLS)
            calls = _tool_calls(message)
            answer = message.get("content")
            if answer is not None and not isinstance(answer, str):
                raise ServerError("Expected text in the assistant message")
            answer = answer or ""
            claims = list(dict.fromkeys(
                " ".join(claim.split()) for claim in _VERIFY_TAG.findall(answer) if claim.strip()
            )) if self.forks else []
            # A claim already checked in this task keeps its verdict, so restating it can end the turn.
            claims = [claim for claim in claims if claim not in self.verdicts]
            if not calls and not answer.strip():
                raise ServerError("Model returned neither an answer nor a tool call")

            leaf = self.tree.append(message)
            await self._observe(leaf, calls, defer=bool(claims), stalls=self.stalls if self.forks else None)
            if claims:
                anchor = self.tree.head
                verdicts = await asyncio.gather(*(self._verify(anchor, claim) for claim in claims))
                self.verdicts.update(zip(claims, verdicts))
                findings = [f"{claim}\n{verdict}" for claim, verdict in zip(claims, verdicts)]
                for verdict in verdicts:
                    self.on_tool(verdict)
                self.tree.append({
                    "role": "user",
                    "content": "Verification results\n\n" + "\n\n".join(findings) + (
                        "\n\nContinue the original task using these findings. Reuse the verdicts for these "
                        "claims. Call the tools for any remaining work, or answer if the task is complete. "
                        "Reissue any deferred calls that are still needed."
                    ),
                })
            elif (self.forks and calls and self.requests_left >= len(LENSES) * _REVIEW_REQUESTS + 2
                  and (stall := self.stalls.stall())):
                self.tree.save()
                await self._reflect(stall)
                reflected = True
            self.tree.save()
            if not claims and not calls:
                return answer.strip()

        raise TurnLimitError(f"Reached the {self.max_turns}-request limit before a final answer. Increase --max-turns.")
