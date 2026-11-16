import asyncio
import json
import re

from .client import ServerError
from .reflection import LENSES, REFLECTION_PROMPT, StallDetector
from .tools import TOOLS


SYSTEM_PROMPT = (
    "You are Reflect, a coding agent working in the current workspace. "
    "Use the provided tools to carry out the user's request. Use workspace-relative paths. "
    "Read a file before editing it and check the result of each tool call. "
    "If a tool returns an error or a nonzero exit code, use that information to recover. "
    "On the main branch, before acting on an unchecked assumption about a file, API, or config key, "
    "emit <verify>specific claim</verify> and wait for the verification verdict. "
    "Prefer existing tools for mechanical work and check the project skills for a relevant workflow. "
    "On the main branch, after confirming a reusable command or workflow, write or update a short Markdown "
    "skill under .refl/skills/ with when to use it and the command that worked. "
    "When the task is complete, give a brief final answer."
)

_VERIFY_TAG = re.compile(r"<verify>(.*?)</verify>", re.DOTALL | re.IGNORECASE)
_READ_ONLY_NAMES = {"read_file", "grep_search"}
_READ_ONLY_TOOLS = [tool for tool in TOOLS if tool["function"]["name"] in _READ_ONLY_NAMES]
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


class TurnLimitError(RuntimeError):
    """The turn used all of its allotted model requests."""


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
    def __init__(self, server, model, workspace, tree, max_turns, max_tokens, on_tool):
        self.server = server
        self.model = model
        self.workspace = workspace
        self.tree = tree
        self.stalls = StallDetector(tree.reflection)
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.on_tool = on_tool
        self.strategy_sections = []
        self.track_strategy = False
        self.strategy_path = f".refl/strategies/{tree.path.stem}.md"

    def _messages(self, leaf, *, system_prompt=None):
        messages = self.tree.messages(leaf)
        skills = self.workspace.skill_paths()
        system = system_prompt or messages[0]["content"]
        system += "\n\n" + _PROJECT_WRITE_RULE
        guidance = self.workspace.project_guidance()
        if guidance:
            system += "\n\nProject guidance from REFLECT.md:\n" + guidance
        if skills:
            system += "\n\nRead relevant project skills before investigating: " + json.dumps(skills, ensure_ascii=False)
        messages[0] = {**messages[0], "content": system}
        return messages

    def _strategy_event(self, text):
        self.strategy_sections.append(text)
        try:
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
                tracking = not read_only and self.track_strategy
                if tracking:
                    try:
                        args = json.loads(function["arguments"])
                    except ValueError:
                        args = None
                    target = (args.get("command") or args.get("path") or "") if isinstance(args, dict) else ""
                    attempt = (" ".join(f"{name} {target}".split()))[:200]
                    self._strategy_event(f"### Attempt\n\n    {attempt}")
                    self.on_tool(f"Strategy\nTrying {attempt}")
                self.on_tool(prefix + name)
                result = await asyncio.to_thread(self.workspace.execute, function)
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
                message = await self.server.complete(
                    self.model, self._messages(leaf, system_prompt=_VERIFY_PROMPT), _READ_ONLY_TOOLS, self.max_tokens,
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
                message = await self.server.complete(
                    self.model, messages, [], self.max_tokens, response_format=_VERDICT_SCHEMA,
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
        except ServerError as error:
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
        reason = "The reviewer reached its three-request limit."
        try:
            for _ in range(3):
                message = await self.server.complete(
                    self.model, self._messages(leaf, system_prompt=REFLECTION_PROMPT),
                    _READ_ONLY_TOOLS, self.max_tokens,
                )
                calls = _tool_calls(message)
                leaf = self.tree.append(message, parent_id=leaf, branch_type="reflection")
                if calls:
                    leaf, _ = await self._observe(leaf, calls, branch_type="reflection")
                    continue
                proposal = message.get("content")
                if isinstance(proposal, str) and proposal.strip():
                    self.tree.finish_fork(leaf, anchor, "merged")
                    return " ".join(proposal.split())[:600]
                reason = "The reviewer returned no proposal."
                break
        except ServerError as error:
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
                + "\n\nChoose an evidence-backed next action. Check whether it advances "
                "the expected result. Use a relevant saved skill if one applies. Update it if the check confirms "
                "a reusable improvement."
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
        if self.tree.head == 0:
            raise ValueError("Start a task before using /reflect.")
        self._close_unfinished_round()
        self.tree.save()
        await self._reflect("Review the current conversation and propose the next action.", manual=True)
        self.tree.append({"role": "assistant", "content": "Reflection complete. The findings are ready for the next task."})
        self.tree.save()

    async def respond(self, prompt):
        self.stalls.start_turn()
        self._close_unfinished_round()
        self.tree.append({"role": "user", "content": prompt})
        self.tree.save()
        try:
            return await self._respond()
        except (ServerError, TurnLimitError) as error:
            self.tree.append({"role": "assistant", "content": f"[Turn stopped] {error}"})
            self.tree.save()
            raise
        finally:
            self.track_strategy = False

    async def _respond(self):
        for _ in range(self.max_turns):
            message = await self.server.complete(
                self.model, self._messages(self.tree.head), TOOLS, self.max_tokens,
            )
            calls = _tool_calls(message)
            answer = message.get("content")
            if answer is not None and not isinstance(answer, str):
                raise ServerError("Expected text in the assistant message")
            answer = answer or ""
            claims = list(dict.fromkeys(claim.strip() for claim in _VERIFY_TAG.findall(answer) if claim.strip()))
            if not calls and not answer.strip():
                raise ServerError("Model returned neither an answer nor a tool call")

            leaf = self.tree.append(message)
            await self._observe(leaf, calls, defer=bool(claims), stalls=self.stalls)
            if claims:
                anchor = self.tree.head
                verdicts = await asyncio.gather(*(self._verify(anchor, claim) for claim in claims))
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
            elif calls and (stall := self.stalls.stall()):
                self.tree.save()
                await self._reflect(stall)
            self.tree.save()
            if not claims and not calls:
                return answer.strip()

        raise TurnLimitError(f"Reached the {self.max_turns}-request limit before a final answer. Increase --max-turns.")
