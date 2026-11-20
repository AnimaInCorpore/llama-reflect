import asyncio
import json
import re

from .client import THINKING_BUDGET_MESSAGE, ContextLimitError, ServerError, TokenLimitError
from .reflection import MANUAL, REFLECTION_PROMPT, TEMPERATURE_SLOTS, StallDetector
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
_NO_VERIFY_PROMPT = _PROMPT_START + _PROMPT_END

_VERIFY_TAG = re.compile(r"<verify>(.*?)</verify>", re.DOTALL | re.IGNORECASE)
_READ_ONLY_NAMES = {"read_file", "grep_search", "view_image"}
_READ_ONLY_TOOLS = [tool for tool in TOOLS if tool["function"]["name"] in _READ_ONLY_NAMES]
_BASH_TOOLS = [tool for tool in TOOLS if tool["function"]["name"] == "bash"]
_REVIEWERS = len(TEMPERATURE_SLOTS[0])
_REVIEW_REQUESTS = 2  # at most, for a role with tools
_ENGINE_PROMPTS = ("Verification results", "Reflection results", "Run the smoke check now.",
                   "Carry out the plan now", "Your last reply reached the limit", "This is the final request",
                   "The request limit of this turn is reached")
# One request beyond the limit, so that a turn that ran out of requests still says where the task stands.
_LIMIT_REPORT_PROMPT = (
    "The request limit of this turn is reached, and no tools remain. Report where the task stands: what is done, "
    "what you found, what is still open, and the next step. The user can continue the task from your report."
)
# The last request after a reflection in a task that changed no file, such as a question.
_FINAL_ANSWER_PROMPT = (
    "This is the final request, and no tools remain. Answer the task now from what you found so far, and say "
    "what is still open."
)
# Sent once per task when a main-branch reply is cut off at --max-tokens. The cut-off reply is discarded.
_CUT_OFF_PROMPT = (
    "Your last reply reached the limit of {limit:,} output tokens before it was complete, so it was discarded. "
    "Think more briefly before you act, and split a large file into several smaller writes."
)
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
_PROPOSAL_PROMPT = (
    "You are a read-only reviewer helping a coding task that stalled. Answer in your role. In proposal, give "
    "one concrete next action that does not contradict the task's stated requirements. In basis, give the "
    "evidence, known technique, or analogy behind it, and say whether it is unverified. In cost, say what "
    "trying it takes. In fails_if, give the result that would show it did not work. Keep each field brief."
)
_FIELD_LABELS = {"abstraction": "Abstract form", "basis": "Basis", "cost": "Cost", "fails_if": "Fails if"}
_AFTER_REFLECTION = (
    "Evidence-based ideas rest on results from this workspace. Unverified ideas have no evidence yet. Start with "
    "the evidence-based idea that explains the most of what you observed, run its check, and act on what it "
    "shows. Try unverified ideas only when the evidence does not settle the question. Do not go back to the "
    "edits or commands you repeated before this reflection. For a localized fix, read only the lines you need "
    "and make a targeted edit. Preserve unrelated code and keep the final request for rerunning the check that "
    "showed the problem."
)
_DECISION_REQUEST = (
    "Before you act, choose the idea to try first. In reason, say which idea you chose and which observations "
    "it explains. In first_step, give the exact action you will take next. In role, name the reviewer whose "
    "idea that is."
)
# The role comes last, so the model names it after writing the plan and the label matches the plan.
_DECISION_FIELDS = ("reason", "first_step", "role")
_CARRY_OUT = "Carry out the plan now, starting with its first step."


def _proposal_schema(fields):
    return {
        "type": "json_object",
        "schema": {
            "type": "object",
            "properties": {field: {"type": "string"} for field in fields},
            "required": list(fields),
            "additionalProperties": False,
        },
    }


def _format_proposal(answer):
    """Turn a structured reviewer answer into one line for the main branch."""
    parts = [" ".join(answer["proposal"].split())]
    for field, label in _FIELD_LABELS.items():
        value = " ".join(str(answer.get(field, "")).split())
        if value:
            parts.append(f"{label}: {value}")
    return " ".join(part if part.endswith((".", "?", "!")) else part + "." for part in parts)


# auto leaves thinking to the chat template, off disables it for every request, and main keeps it
# only for main-branch requests.
THINKING_MODES = ("auto", "off", "main")
def default_thinking_budget(max_tokens):
    """Give half of the output limit to thinking, so the answer keeps the other half.

    An answer can be long: writing a 5 KB SVG file took a 27B model about 2,600 tokens.
    """
    return max_tokens // 2


class TurnLimitError(RuntimeError):
    """The turn used all of its allotted model requests. `report` holds the model's account of where the task
    stands, from one more request without tools, when that request succeeded."""

    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report


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


def _excerpt(value, limit):
    # Keep quotes and backslashes as they are, so a model that copies the text gets the file's characters.
    text = str(value or "").replace("\r", "\\r").replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _action(function):
    """Describe a main-branch call for a reviewer summary, including what a write or edit changed."""
    summary = _call_summary(function)
    try:
        args = json.loads(function["arguments"])
    except (TypeError, ValueError):
        return summary
    if not isinstance(args, dict):
        return summary
    if function["name"] == "write_file":
        return f"{summary} (content: {_excerpt(args.get('content'), 300)})"
    if function["name"] == "edit_file":
        return (f"{summary} (replaced \"{_excerpt(args.get('old_text'), 120)}\" "
                f"with \"{_excerpt(args.get('new_text'), 180)}\")")
    return summary


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


def tool_text(content):
    """Show a stored JSON tool result to the model as plain text.

    Sessions keep tool results as JSON. In a JSON string, file contents appear with escapes such as \\" and \\n,
    and models copy those escapes into edits that then do not match the file.
    """
    try:
        result = json.loads(content)
    except (TypeError, ValueError):
        return content
    if not isinstance(result, dict) or not result or not set(result) <= {"output", "error", "exit_code", "changed",
                                                                          "image"}:
        return content
    lines = []
    if "error" in result:
        lines.append(f"Error: {result['error']}")
    if "exit_code" in result:
        lines.append(f"Exit code: {result['exit_code']}")
    if "output" in result:
        lines.append(str(result["output"]))
    return "\n".join(lines)


def tool_image(content):
    """Return the data URL of the image in a stored view_image result, or None."""
    try:
        result = json.loads(content)
    except (TypeError, ValueError):
        return None
    image = result.get("image") if isinstance(result, dict) else None
    return image if isinstance(image, str) and image.startswith("data:image/") else None


def attach_images(messages, vision):
    """Show each tool result as text, and the images from view_image in a user message after the results.

    Every chat template with image input accepts images in a user message, while not every one renders them in
    a tool result. Without image input, a note replaces the images.
    """
    converted, images = [], []

    def flush():
        if not images:
            return
        if not vision:
            converted.append({"role": "user", "content": "The server has no image input, so the images from "
                                                          "view_image are not shown."})
        else:
            caption = ("The image from the view_image call above." if len(images) == 1 else
                       f"The {len(images)} images from the view_image calls above, in order.")
            converted.append({"role": "user", "content": [
                {"type": "text", "text": caption},
                *({"type": "image_url", "image_url": {"url": image}} for image in images),
            ]})
        images.clear()

    for message in messages:
        if message["role"] == "tool":
            content = message.get("content")
            converted.append({**message, "content": tool_text(content)})
            if image := tool_image(content):
                images.append(image)
            continue
        flush()
        converted.append(message)
    flush()
    return converted


def split_text_and_calls(messages):
    """Send an assistant's text and its tool calls as two consecutive assistant messages."""
    split = []
    for message in messages:
        if message["role"] == "assistant" and message.get("tool_calls") and str(message.get("content") or "").strip():
            split.append({key: value for key, value in message.items() if key != "tool_calls"})
            split.append({"role": "assistant", "content": None, "tool_calls": message["tool_calls"]})
        else:
            split.append(message)
    return split


# Some chat templates, such as Gemma 4's, render an assistant's text after the results of its tool calls and then
# close the turn. The model then sees its plan after the result and repeats the call. Rendering this probe through
# /apply-template shows whether the server's template does that.
_PROBE_TEXT = "reflect-probe-text"
_PROBE_RESULT = "reflect-probe-result"
_TEMPLATE_PROBE = [
    {"role": "user", "content": "Read probe.txt."},
    {"role": "assistant", "content": _PROBE_TEXT, "tool_calls": [{
        "id": "probe", "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": "probe.txt"})},
    }]},
    {"role": "tool", "tool_call_id": "probe", "content": _PROBE_RESULT},
]


# Shares of the server's context at which the main conversation triggers a warning.
_CONTEXT_WARNINGS = (0.75, 0.9)


class Engine:
    def __init__(self, server, model, workspace, tree, max_turns, max_tokens, on_tool, *, forks=True,
                 verification=True, thinking="auto", thinking_budget=None, fork_effort=None, context_size=None,
                 vision=False):
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
        self.verification = forks and verification
        if thinking not in THINKING_MODES:
            raise ValueError(f"Unknown thinking mode {thinking!r}")
        self.thinking = thinking
        # Thinking tokens per request before the server ends the thinking block. -1 sets no limit.
        if thinking_budget is None:
            thinking_budget = default_thinking_budget(max_tokens)
        self.thinking_budget = thinking_budget if thinking_budget >= 0 else None
        # Reasoning effort for verifier and reviewer requests that think, such as "low". None keeps the
        # template's default.
        self.fork_effort = fork_effort
        self.strategy_sections = []
        self.track_strategy = False
        self.strategy_path = f".refl/strategies/{tree.path.stem}.md"
        # Verdicts from the current task, reused until a main-branch command or write can change the evidence.
        self.verdicts = {}
        # Whether a main-branch tool changed a file in the current task. A task without changes, such as a
        # question, uses its last request after a reflection for the answer instead of a smoke check.
        self.wrote_files = False
        # Whether the chat template moves an assistant's text after its tool results. Checked on the first request.
        self.split_text = None
        # Tokens of context per server slot, if the server reports it, and how many warnings were shown.
        self.context_size = context_size
        self.context_warnings = 0
        # Whether the server takes image input. Without it the model does not get the view_image tool.
        self.vision = vision

    def _messages(self, leaf, *, system_prompt=None):
        messages = self.tree.messages(leaf)
        skills = self.workspace.skill_paths()
        # Build the main prompt for each request so resumed sessions, --linear, and --no-verify use the
        # current instructions.
        system = system_prompt or (SYSTEM_PROMPT if self.verification else _NO_VERIFY_PROMPT)
        system += "\n\n" + _PROJECT_WRITE_RULE
        guidance = self.workspace.project_guidance()
        if guidance:
            system += "\n\nProject guidance from REFLECT.md:\n" + guidance
        if skills:
            system += "\n\nRead relevant project skills before investigating: " + json.dumps(skills, ensure_ascii=False)
        messages[0] = {**messages[0], "content": system}
        return messages

    def _brief_messages(self, leaf, start, system):
        """Messages for a role that sees only its own branch from `start`, without the main conversation."""
        guidance = self.workspace.project_guidance()
        if guidance:
            system += "\n\nProject guidance from REFLECT.md:\n" + guidance
        return [{"role": "system", "content": system}, *self.tree.messages(leaf, start=start)]

    def _brief(self, anchor, reason):
        """Summarize the task, the reason for reflecting, and the latest main-branch actions."""
        messages = self.tree.messages(anchor)
        task = next((message["content"] for message in reversed(messages)
                     if message["role"] == "user" and isinstance(message.get("content"), str)
                     and not message["content"].startswith(_ENGINE_PROMPTS)), "")
        functions, actions = {}, []
        for message in messages:
            if message["role"] == "assistant":
                for call in message.get("tool_calls") or []:
                    functions[call["id"]] = call["function"]
            elif message["role"] == "tool" and message.get("tool_call_id") in functions:
                result = " ".join(str(tool_text(message.get("content", ""))).split())
                actions.append(f"- {_action(functions[message['tool_call_id']])}\n  {result[:300]}")
        parts = [f"Task: {task[:1500]}", f"Reflection reason: {reason}"]
        if actions:
            parts.append("Latest actions and results:\n" + "\n".join(actions[-6:]))
        entries = self.workspace.top_level_entries()
        if entries:
            parts.append("Workspace top level: " + ", ".join(entries))
        return "\n\n".join(parts)

    async def _complete(self, messages, tools, *, fork=False, response_format=None, temperature=0.0,
                        tool_choice=None):
        if self.requests_left is not None:
            if self.requests_left <= (1 if fork else 0):
                raise TurnLimitError("No model requests remain for this step.")
            self.requests_left -= 1
        thinking = self.thinking == "auto" or (self.thinking == "main" and not fork)
        if self.split_text is None:
            self.split_text = await self._template_moves_text()
        messages = attach_images(messages, self.vision)
        if not self.vision:
            tools = [tool for tool in tools if tool["function"]["name"] != "view_image"]
        if self.split_text:
            messages = split_text_and_calls(messages)
        try:
            message, tokens = await self.server.complete(self.model, messages, tools, self.max_tokens,
                                                         response_format=response_format, temperature=temperature,
                                                         thinking=thinking, tool_choice=tool_choice,
                                                         thinking_budget=self.thinking_budget,
                                                         reasoning_effort=self.fork_effort if fork else None)
        except ContextLimitError as error:
            raise ContextLimitError(error.tokens, error.n_ctx, self._context_advice()) from None
        reasoning = message.get("reasoning_content")
        if thinking and isinstance(reasoning, str) and reasoning.rstrip().endswith(THINKING_BUDGET_MESSAGE):
            self.on_tool(f"Thinking stopped at its budget of {self.thinking_budget:,} tokens")
        if not fork and tokens and self.context_size:
            self._check_context(tokens)
        return message

    def _context_advice(self):
        return (f"Reflect does not shorten conversations yet. For more room, restart llama-server with more "
                f"context per slot, such as a larger -c or a smaller -np, and continue with "
                f"--resume {self.tree.path.stem}.")

    def _check_context(self, tokens):
        """Warn once as the main conversation passes each share of the server's context."""
        share = tokens / self.context_size
        reached = sum(share >= limit for limit in _CONTEXT_WARNINGS)
        if reached > self.context_warnings:
            self.context_warnings = reached
            self.on_tool(f"Context: {tokens:,} of {self.context_size:,} tokens in use ({share:.0%}). "
                         f"{self._context_advice()}")

    async def _template_moves_text(self):
        """Check whether the server's chat template renders an assistant's text after its tool results."""
        try:
            prompt = await self.server.apply_template(_TEMPLATE_PROBE, TOOLS)
        except ServerError:
            return False
        result = prompt.find(_PROBE_RESULT)
        return 0 <= result < prompt.find(_PROBE_TEXT)

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
                if not read_only and result.get("changed") is True:
                    self.wrote_files = True
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

    async def _review(self, anchor, stall, role, brief, history):
        self.on_tool(f"Reviewing as {role.name}: {role.focus}")
        request = f"Reflection role: {role.name}\nReview reason: {stall}\n\n{role.focus}"
        if role.brief:
            request = f"{brief}\n\n{request}"
        skill = None
        if role.name == "Existing patterns":
            if history:
                request += "\n\nRecent project commits\n" + history
            paths = self.workspace.skill_paths()
            user_messages = (message.get("content", "") for message in reversed(self.tree.messages(anchor))
                             if message["role"] == "user")
            skill = next((path for content in user_messages if isinstance(content, str) for path in paths
                          if path in content), None)
            if skill is None and len(paths) == 1:
                skill = paths[0]
        start = leaf = self.tree.append({"role": "user", "content": request}, parent_id=anchor,
                                        branch_type="reflection")
        if skill:
            call = {
                "type": "function",
                "id": f"s{leaf:08x}",
                "function": {"name": "read_file", "arguments": json.dumps({"path": skill})},
            }
            leaf = self.tree.append({"role": "assistant", "content": None, "tool_calls": [call]},
                                    parent_id=leaf, branch_type="reflection")
            leaf, _ = await self._observe(leaf, [call], branch_type="reflection")

        def context(leaf, prompt):
            system = f"{prompt}\n\nYour role: {role.name}. {role.focus}"
            return self._brief_messages(leaf, start, system) if role.brief else self._messages(leaf, system_prompt=system)

        reason = "The reviewer returned no proposal."
        try:
            proposal = None
            if role.tools:
                # One request to gather evidence. Requiring a call keeps a small model from proposing a check
                # instead of running it. A server that ignores tool_choice can still answer in text.
                message = await self._complete(context(leaf, REFLECTION_PROMPT), _READ_ONLY_TOOLS, fork=True,
                                               temperature=role.temperature, tool_choice="required")
                calls = _tool_calls(message)
                leaf = self.tree.append(message, parent_id=leaf, branch_type="reflection")
                if calls:
                    leaf, _ = await self._observe(leaf, calls, branch_type="reflection")
                    leaf = self.tree.append({
                        "role": "user",
                        "content": "Give your answer now using the evidence already in the conversation.",
                    }, parent_id=leaf, branch_type="reflection")
                else:
                    proposal = " ".join(str(message.get("content") or "").split())
            if proposal is None:
                message = await self._complete(
                    context(leaf, _PROPOSAL_PROMPT), [], fork=True,
                    response_format=_proposal_schema(role.fields), temperature=role.temperature,
                )
                if _tool_calls(message):
                    raise ServerError("The reviewer called a tool on its final request.")
                leaf = self.tree.append(message, parent_id=leaf, branch_type="reflection")
                try:
                    proposal = _format_proposal(json.loads(message.get("content") or ""))
                except (AttributeError, KeyError, TypeError, ValueError):
                    proposal = ""
            if proposal.strip(" ."):
                self.tree.finish_fork(leaf, anchor, "merged")
                return proposal[:800]
        except (ServerError, TurnLimitError) as error:
            reason = str(error)

        self.tree.finish_fork(leaf, anchor, "pruned")
        self.on_tool(f"Reflection {role.name}: {reason}")
        return None

    async def _reflect(self, reason, *, manual=False):
        anchor = self.tree.head
        heading = "Request" if manual else "Observed stall"
        history = await asyncio.to_thread(self.workspace.recent_commits)
        unavailable = set() if history or self.workspace.skill_paths() else {"Existing patterns"}
        level, roles = self.stalls.plan(MANUAL if manual else self.stalls.kind, unavailable=unavailable,
                                        manual=manual)
        names = ", ".join(role.name for role in roles)
        self._strategy_event(
            f"## Reflection\n\n{heading}\n\n    {' '.join(reason.split())}\n\nTemperature {level}: {names}")
        self.on_tool(f"Strategy\nReflecting: {reason}\nTemperature {level}: {names}\nTracking: {self.strategy_path}")
        brief = self._brief(anchor, reason)
        proposals = await asyncio.gather(*(self._review(anchor, reason, role, brief, history) for role in roles))
        found = [(role, proposal) for role, proposal in zip(roles, proposals) if proposal]
        # Grounded roles inspect the workspace, so their ideas come first and are labeled as evidence-based.
        sections = []
        for title, grounded in (("Evidence-based ideas", True), ("Unverified ideas", False)):
            entries = [f"- {role.name}: {proposal}" for role, proposal in found if (role.group == "grounded") == grounded]
            if entries:
                sections.append(title + "\n" + "\n".join(entries))
        summary = "\n\n".join(sections) or "The reviewers returned no usable proposals."
        self.on_tool(f"Strategy\nReflection proposals\n{summary}")
        self._strategy_event("### Proposals\n\n" + summary)
        self.track_strategy = True
        decide = bool(found) and not manual
        content = f"Reflection results\n{heading}: {reason}\n\n{summary}\n\n{_AFTER_REFLECTION}"
        self.tree.append({"role": "user", "content": content + ("\n\n" + _DECISION_REQUEST if decide else "")})
        if decide:
            await self._decide([role.name for role, _ in found])

    async def _decide(self, names):
        """Have the main branch commit to one reviewer's idea before it acts, without tools."""
        schema = _proposal_schema(_DECISION_FIELDS)
        schema["schema"]["properties"]["role"] = {"type": "string", "enum": names}
        try:
            message = await self._complete(self._messages(self.tree.head), [], response_format=schema)
            decision = json.loads(message.get("content") or "")
            plan = (f"Plan: try the {decision['role']} idea first. Reason: {' '.join(decision['reason'].split())} "
                    f"First step: {' '.join(decision['first_step'].split())}")
        except (AttributeError, KeyError, TypeError, ValueError, ServerError, TurnLimitError) as error:
            self.on_tool(f"Strategy\nNo plan after reflection: {error}")
            self.tree.append({"role": "assistant", "content": "I will follow the reflection results."})
        else:
            self.on_tool(f"Strategy\n{plan}")
            self._strategy_event(f"### Plan\n\n{plan}")
            self.tree.append({"role": "assistant", "content": plan})
        self.tree.append({"role": "user", "content": _CARRY_OUT})

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
        self.wrote_files = False
        self._close_unfinished_round()
        self.tree.append({"role": "user", "content": prompt})
        self.tree.save()
        self.requests_left = self.max_turns
        try:
            return await self._respond()
        except (ServerError, TurnLimitError) as error:
            note = f"[Turn stopped] {error}"
            # The report and the note form one reply, so the conversation keeps alternating between roles.
            report = getattr(error, "report", None)
            self.tree.append({"role": "assistant", "content": f"{report}\n\n{note}" if report else note})
            self.tree.save()
            raise
        finally:
            self.track_strategy = False
            self.requests_left = None

    async def _final_answer(self):
        """Ask for the answer on the last request of a task that changed no file, where a smoke check has nothing to
        check and would replace the answer to a question with a command's output."""
        self.tree.append({"role": "user", "content": _FINAL_ANSWER_PROMPT})
        message = await self._complete(self._messages(self.tree.head), [])
        answer = message.get("content")
        if _tool_calls(message) or not isinstance(answer, str) or not answer.strip():
            raise ServerError("Expected a text answer on the final request.")
        self.tree.append(message)
        self.tree.save()
        return answer.strip()

    async def _smoke_check(self):
        self.tree.append({
            "role": "user",
            "content": (
                "Run the smoke check now. Use one bash call with the command that reproduced the failure "
                "or the applicable check from the reflection findings. Run only the check: change no files, "
                "and put no other command after it, so that the call's exit code is the check's. This is the "
                "final request. If there is no applicable smoke check, say so."
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
        cut_off = False
        while self.requests_left:
            if reflected and self.requests_left == 1:
                return await (self._smoke_check() if self.wrote_files else self._final_answer())
            try:
                message = await self._complete(self._messages(self.tree.head), TOOLS)
            except TokenLimitError:
                # A reply cut off mid-way, often while writing a large file, is lost. Ask once for a shorter one.
                if cut_off:
                    raise
                cut_off = True
                self.on_tool(f"The reply reached the {self.max_tokens:,}-token limit. Asking for a shorter one.")
                self.tree.append({"role": "user", "content": _CUT_OFF_PROMPT.format(limit=self.max_tokens)})
                self.tree.save()
                continue
            calls = _tool_calls(message)
            answer = message.get("content")
            if answer is not None and not isinstance(answer, str):
                raise ServerError("Expected text in the assistant message")
            answer = answer or ""
            claims = list(dict.fromkeys(
                " ".join(claim.split()) for claim in _VERIFY_TAG.findall(answer) if claim.strip()
            )) if self.verification else []
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
            elif (self.forks and self.requests_left >= _REVIEWERS * _REVIEW_REQUESTS + 3
                  # A final answer while a rerun check still fails is a stall too: the model gave up.
                  and (stall := self.stalls.stall() if calls else self.stalls.stopped())):
                self.tree.save()
                await self._reflect(stall)
                reflected = True
                if not calls:
                    self.tree.save()
                    continue
            self.tree.save()
            if not claims and not calls:
                return answer.strip()

        raise TurnLimitError(f"Reached the {self.max_turns}-request limit before a final answer. Send a follow-up "
                             "such as \"continue\" to go on, or raise --max-turns.", report=await self._limit_report())

    async def _limit_report(self):
        """Ask once more, without tools, where the task stands. The request comes on top of the limit, so the work
        keeps all of its requests. Without a usable reply the turn stops without a report."""
        self.tree.append({"role": "user", "content": _LIMIT_REPORT_PROMPT})
        self.tree.save()
        self.requests_left = 1
        try:
            message = await self._complete(self._messages(self.tree.head), [])
        except ServerError:
            return None
        finally:
            self.requests_left = 0
        answer = message.get("content")
        if _tool_calls(message) or not isinstance(answer, str) or not answer.strip():
            return None
        return answer.strip()
