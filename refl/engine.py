import asyncio
import json
import re
import time
from pathlib import PurePosixPath

from .challenge import ANSWER_PROMPT, CHALLENGE_PROMPT, CRITICS, EDIT_TOOLS
from .challenge import REQUESTS as CHALLENGE_REQUESTS
from .challenge import SIMPLIFIER, SIMPLIFY_ANSWER_PROMPT, SIMPLIFY_PROMPT
from .challenge import answer_schema, format_objection, format_simplification, objection_of, simplification_of
from .challenge import SMALL_EDIT_CHARS, SMALL_EDIT_LINES, simplification_claim, simplify_schema, small_edit
from .client import THINKING_BUDGET_MESSAGE, ContextLimitError, ServerError, TokenLimitError
from .reflection import FACTS_RULE, GOAL_RULE, HONESTY_RULE, MANUAL, REFLECTION_PROMPT, StallDetector
from .moderator import CHECK_PROMPT, CHECK_SCHEMA, CHOICE_PROMPT, MODERATOR_PROMPT, RULING_PROMPT
from .moderator import case, choice_schema, ruling_schema, rulings_of
from .reflection import REQUESTS as REFLECTION_REQUESTS
from .style import CHAT_MARK
from .tools import TOOLS
from .tree import ensure_git_ignored



def _short(text, limit=60):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


# Hidden folders that hold project configuration rather than scratch files.
_PROJECT_FOLDERS = {".github", ".gitlab", ".circleci", ".devcontainer", ".vscode", ".husky", ".config", ".cargo"}


def _scratch(path):
    """Whether `path` lies in a hidden folder that holds scratch files or Reflect's own notes, such as .refl/.

    In a real session, a test harness in .tmpcheck/ got a full plan check, and nothing held up.
    """
    parts = PurePosixPath(path.replace("\\", "/")).parts
    return len(parts) > 1 and parts[0].startswith(".") and parts[0] not in _PROJECT_FOLDERS


def _without_images(messages, main):
    """Leave out the images of the main branch's view_image results, of the messages where `main` is true.

    A fork judges the plan or a claim and does not need the pixels. In a real session, every critic and verifier
    request carried a 600 KB screenshot. The fork can look at the image itself with view_image.
    """
    stripped = []
    for message, on_main in zip(messages, main):
        if on_main and message["role"] == "tool" and tool_image(message.get("content")):
            result = json.loads(message["content"])
            result.pop("image")
            result["output"] = (f"{result.get('output', '')}\n[The image went to the main branch. Use view_image to "
                                f"look at it yourself.]").strip()
            message = {**message, "content": json.dumps(result, ensure_ascii=False)}
        stripped.append(message)
    return stripped


def _elapsed(start):
    seconds = round(time.monotonic() - start)
    return f"{seconds} s" if seconds < 60 else f"{seconds // 60} min {seconds % 60} s"


# Where the answer that ended the latest turn is kept, in the session folder, which Git ignores.
_LAST_REPORT = "last-report.md"

_PROMPT_START = (
    "You are Reflect, a coding agent working in the current workspace. "
    "Use the provided tools to carry out the user's request. Use workspace-relative paths. "
    "Talk to the user while you work: before each group of tool calls, tell the user in one or two short "
    "sentences what you are about to do and why, and say briefly what you found when a result matters. "
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
    "To recall earlier work in this workspace, read the skills and the notes under .refl/strategies/ first. The "
    "session logs under .refl/sessions/ are large raw JSON. "
    "When the task is complete, give a brief final answer."
)
SYSTEM_PROMPT = _PROMPT_START + _VERIFICATION_INSTRUCTION + _PROMPT_END
_NO_VERIFY_PROMPT = _PROMPT_START + _PROMPT_END

_VERIFY_TAG = re.compile(r"<verify>(.*?)</verify>", re.DOTALL | re.IGNORECASE)
_READ_ONLY_NAMES = {"read_file", "grep_search", "view_image"}
_READ_ONLY_TOOLS = [tool for tool in TOOLS if tool["function"]["name"] in _READ_ONLY_NAMES]
_BASH_TOOLS = [tool for tool in TOOLS if tool["function"]["name"] == "bash"]
# Requests of the turn that a reflection needs: the decision, one step on it, and a final check. The reviewers
# and their verifiers have a budget of their own.
_ACT_ON_REFLECTION = 3
_ENGINE_PROMPTS = ("Verification results", "Reflection results", "Plan challenge", "Not followed yet",
                   "For the user", "Run the smoke check now.", "Carry out the moderator's plan now", "Carry out the revised plan now", "Your last reply reached the limit",
                   "This is the final request", "The request limit of this turn is reached")
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
# Every role gets this rule, so that no reviewer or critic proposes what the main branch may not do.
_DEPENDENCY_RULE = (
    "Solve the task with the tools and libraries that are already available in the workspace and on this system. "
    "Do not install packages or add dependencies. If the task needs a publicly trusted tool or library that is "
    "missing, stop and ask the user for it in your answer before trying it."
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
    "one concrete next action that does not contradict the task's stated requirements, or leave it empty if the "
    "evidence supports none. In facts, give the facts in the workspace as it is now that the proposal rests on, "
    "with file and line. In basis, give the reasoning, known technique, or analogy that connects those "
    "facts to the proposal. In cost, say what trying it takes. In fails_if, give the result that would show it "
    "did not work. Keep each field brief. " + FACTS_RULE + " " + GOAL_RULE + " " + HONESTY_RULE
)
_FIELD_LABELS = {"abstraction": "Abstract form", "facts": "Facts", "basis": "Basis", "cost": "Cost",
                 "fails_if": "Fails if"}
_AFTER_REFLECTION = (
    "A verifier confirmed the facts behind each idea in this workspace. Start with the idea that explains the "
    "most of what you observed, run its check, and act on what it shows. Do not go back to the edits or "
    "commands you repeated before this reflection. For a localized fix, read only the lines you need "
    "and make a targeted edit. Preserve unrelated code and keep the final request for rerunning the check that "
    "showed the problem."
)
_CARRY_OUT = "Carry out the moderator's plan now, starting with its first step."
_FOLLOW_REQUEST = (
    "Revise your plan so that it follows each of these findings and still serves the user's request. Then give "
    "the first step of the revised plan. Do not call tools in this reply."
)
_NOT_FOLLOWED_REQUEST = (
    "The moderator found that your revised plan does not follow its rulings yet: {reason} Revise the plan so "
    "that it does and still serves the user's request. Then give the first step. Do not call tools in this reply."
)
# When a confirmed fact conflicts with the request as stated, or the plan cannot follow the rulings, the user
# decides how the request is meant.
_ASK_USER_REQUEST = (
    "Do not make the change. Write a short message to the user instead. Say what you were about to do and the "
    "confirmed fact that stands against it. Then ask one or two precise questions about how the user wants the "
    "request specified, with the options you see. Do not call tools in this reply."
)
_AFTER_CHALLENGE = "Carry out the revised plan now. Reissue the deferred calls that are still needed."


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
# The default thinking budget of a fork request. Critics and verifiers averaged about 1,000 tokens of thinking.
FORK_THINKING_BUDGET = 4096


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
                 vision=False, challenge=True, on_progress=None, fork_thinking_budget=None):
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
        # Whether critics challenge the plan before the first file change of a task, and whether they did.
        self.challenge = forks and challenge
        self.challenged = False
        # Requests left in the running plan challenge or reflection batch, which has a budget of its own, and
        # its name for the message when the budget runs out. None outside one.
        self.batch_left = None
        self.batch_name = None
        if thinking not in THINKING_MODES:
            raise ValueError(f"Unknown thinking mode {thinking!r}")
        self.thinking = thinking
        # Thinking tokens per request before the server ends the thinking block. -1 sets no limit.
        if thinking_budget is None:
            thinking_budget = default_thinking_budget(max_tokens)
        self.thinking_budget = thinking_budget if thinking_budget >= 0 else None
        # Thinking tokens per fork request: critics, verifiers, reviewers, and the moderator answer one narrow
        # question. In one session, a verdict on a single claim thought about 9,000 tokens. -1 sets no limit.
        if fork_thinking_budget is None:
            fork_thinking_budget = FORK_THINKING_BUDGET if thinking_budget < 0 else min(FORK_THINKING_BUDGET,
                                                                                         thinking_budget)
        self.fork_thinking_budget = fork_thinking_budget if fork_thinking_budget >= 0 else None
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
        # Called with the phase and its counts, such as critics=(4, 6), for a status line. Counts per phase.
        self.on_progress = on_progress
        self.counts = {}

    def _progress(self, **fields):
        """Report the phase, which resets its counts, or parts of it, such as moderator="ruling"."""
        if "phase" in fields:
            self.counts = {}
        for name, value in fields.items():
            if isinstance(value, tuple):
                self.counts[name] = list(value)
        if self.on_progress is not None:
            self.on_progress(**fields)

    def _actor(self, name, doing, tone):
        """Report what one reflector, the moderator, or the main branch does now, for a status panel.

        The tone is wait, work, good, bad, or quiet, such as work for "reading SPEC.md" and good for "proven".
        """
        self._progress(actor=(name, doing, tone))

    def _stream_to_panel(self, actor):
        """Return on_text for a streamed request of `actor` that reports its progress, or None without a panel."""
        if self.on_progress is None or not actor:
            return None
        state = {"tokens": 0, "start": None, "kind": "waiting", "tail": ""}

        def on_text(kind, text):
            now = time.monotonic()
            if kind == "done":
                state["kind"] = "done"
            else:
                state["start"] = state["start"] or now
                if kind != state["kind"]:
                    state["kind"], state["tail"] = kind, ""
                # A streamed piece is about one token: 73 pieces carried 76 tokens on Basalt.
                state["tokens"] += 1
                state["tail"] = (state["tail"] + text)[-200:]
            elapsed = now - state["start"] if state["start"] else 0
            rate = (state["tokens"] - 1) / elapsed if elapsed > 0 else 0.0
            self._progress(stream=(actor, state["kind"], state["tokens"], rate, state["tail"]))

        self._progress(stream=(actor, "waiting", 0, 0.0, ""))
        return on_text

    def _tick(self, name, done=1, total=0):
        """Count one more finished, or with total=1 one more started, reflector of a kind."""
        count = self.counts.setdefault(name, [0, 0])
        count[0] += done
        count[1] += total
        self._progress(**{name: tuple(count)})

    def _last_report_note(self):
        """A note on the kept report of the latest turn, for the main branch's system prompt, or ""."""
        if not (self.tree.path.parent / _LAST_REPORT).is_file():
            return ""
        return (f"\n\nThe answer that ended the latest turn in this workspace, possibly in an earlier session, is "
                f"in .refl/sessions/{_LAST_REPORT}. Read it first when the user asks to resume or continue.")

    def _messages(self, leaf, *, system_prompt=None):
        messages = self.tree.messages(leaf)
        if system_prompt is not None:
            # A fork: the images that the main branch viewed stay with the main branch.
            main, node = [], leaf
            while node is not None:
                main.append(self.tree.nodes[node].branch_type == "main")
                node = self.tree.nodes[node].parent_id
            messages = _without_images(messages, list(reversed(main)))
        skills = self.workspace.skill_paths()
        # Build the main prompt for each request so resumed sessions, --linear, and --no-verify use the
        # current instructions.
        system = system_prompt or (SYSTEM_PROMPT if self.verification else _NO_VERIFY_PROMPT)
        system += "\n\n" + _PROJECT_WRITE_RULE + "\n\n" + _DEPENDENCY_RULE
        if system_prompt is None:
            system += self._last_report_note()
        guidance = self.workspace.project_guidance()
        if guidance:
            system += "\n\nProject guidance from REFLECT.md:\n" + guidance
        if skills:
            system += "\n\nRead relevant project skills before investigating: " + json.dumps(skills, ensure_ascii=False)
        messages[0] = {**messages[0], "content": system}
        return messages

    def _brief_messages(self, leaf, start, system):
        """Messages for a role that sees only its own branch from `start`, without the main conversation."""
        system += "\n\n" + _DEPENDENCY_RULE
        guidance = self.workspace.project_guidance()
        if guidance:
            system += "\n\nProject guidance from REFLECT.md:\n" + guidance
        return [{"role": "system", "content": system}, *self.tree.messages(leaf, start=start)]

    def _user_request(self, anchor):
        """The user's latest request on the path to `anchor`, without the messages that Reflect adds."""
        return next((message["content"] for message in reversed(self.tree.messages(anchor))
                     if message["role"] == "user" and isinstance(message.get("content"), str)
                     and not message["content"].startswith(_ENGINE_PROMPTS)), "")

    def _brief(self, anchor, reason):
        """Summarize the task, the reason for reflecting, and the latest main-branch actions."""
        messages = self.tree.messages(anchor)
        task = self._user_request(anchor)
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
                        tool_choice=None, branch=None, actor=None):
        """Request a completion. `branch` names a fork by the ID of its first node, so it gets its own session.

        `actor` names who asks, such as a critic, for the status panel. While a panel listens, the reply streams,
        and each piece reports that actor's tokens, speed, and latest text.
        """
        if self.batch_left is not None:
            if self.batch_left <= 0:
                raise TurnLimitError(f"The {self.batch_name} used all of its requests.")
            self.batch_left -= 1
        elif self.requests_left is not None:
            if self.requests_left <= (1 if fork else 0):
                raise TurnLimitError("No model requests remain for this step.")
            self.requests_left -= 1
        thinking = self.thinking == "auto" or (self.thinking == "main" and not fork)
        budget = self.fork_thinking_budget if fork else self.thinking_budget
        if not fork and self.batch_name is None:
            self._progress(phase="Next step, thinking" if thinking else "Next step")
        if self.split_text is None:
            self.split_text = await self._template_moves_text()
        messages = attach_images(messages, self.vision)
        if not self.vision:
            tools = [tool for tool in tools if tool["function"]["name"] != "view_image"]
        if self.split_text:
            messages = split_text_and_calls(messages)
        try:
            stream = self._stream_to_panel(actor or (None if fork else "Main branch"))
            try:
                message, tokens = await self.server.complete(self.model, messages, tools, self.max_tokens,
                                                             response_format=response_format, temperature=temperature,
                                                             thinking=thinking, tool_choice=tool_choice,
                                                             thinking_budget=budget,
                                                             reasoning_effort=self.fork_effort if fork else None,
                                                             session=self._session(branch),
                                                             **({"on_text": stream} if stream else {}))
            finally:
                if stream:
                    stream("done", "")
        except ContextLimitError as error:
            raise ContextLimitError(error.tokens, error.n_ctx, self._context_advice(),
                                    reply_tokens=error.reply_tokens) from None
        reasoning = message.get("reasoning_content")
        if not fork and isinstance(reasoning, str) and reasoning.strip():
            # The main branch's thinking, shown when its reply arrives. Requests do not stream, so not sooner.
            self.on_tool("Thinking\n" + reasoning.strip())
        if thinking and isinstance(reasoning, str) and reasoning.rstrip().endswith(THINKING_BUDGET_MESSAGE):
            self.on_tool(f"Thinking stopped at its budget of {budget:,} tokens")
        if not fork and tokens and self.context_size:
            self._check_context(tokens)
        return message

    def _session(self, branch):
        """The x-session-id of the main branch or of the fork that starts at node `branch`."""
        session = self.tree.path.stem
        return session if branch is None else f"{session}-{branch}"

    def _context_advice(self):
        return (f"Reflect does not shorten conversations yet. For more room, restart llama-server with more "
                f"context per slot, such as a larger -c or a smaller -np, and continue with "
                f"--resume {self.tree.path.stem}.")

    def _check_context(self, tokens):
        """Warn once as the main conversation passes each share of the room it has in the server's context.

        A reply of up to --max-tokens needs room in the same context, and Basalt rejects a request whose prompt
        and max_tokens together exceed it. So the room for the conversation is the context less one reply,
        unless that would leave less than half. With 32,768 of 131,072 tokens, requests fail at 98,304, where a
        warning at 75% of the whole context would only just appear.
        """
        room = self.context_size - self.max_tokens if self.max_tokens * 2 <= self.context_size else self.context_size
        share = tokens / room
        reached = sum(share >= limit for limit in _CONTEXT_WARNINGS)
        if reached > self.context_warnings:
            self.context_warnings = reached
            reserve = (f", keeping {self.max_tokens:,} of the {self.context_size:,} for a reply"
                       if room < self.context_size else "")
            self.on_tool(f"Context: {tokens:,} of {room:,} tokens in use ({share:.0%}){reserve}. "
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

    async def _observe(self, leaf, calls, *, branch_type="main", defer=None, stalls=None):
        """Run the calls, or answer each with a note to reissue it after reviewing `defer`, if given."""
        read_only = branch_type != "main"
        prefix = {"verification": "verify ", "reflection": "reflect ", "challenge": "challenge "}.get(branch_type, "")
        evidence = []
        for call in calls:
            function = call["function"]
            name = function["name"]
            if defer and name not in _READ_ONLY_NAMES:
                # Reads change nothing, so they run now. In one session a grep held back with an edit had to be
                # reissued after the plan check.
                result = {"error": f"Tool call deferred. Reissue it after reviewing {defer}."}
            elif read_only and name not in _READ_ONLY_NAMES:
                result = {"error": f"{name} is unavailable in a read-only {branch_type} fork."}
            else:
                summary = _call_summary(function)
                tracking = not read_only and self.track_strategy
                if tracking:
                    self._strategy_event(f"### Attempt\n\n    {summary}")
                self.on_tool(prefix + summary)
                if not read_only:
                    self._progress(phase=f"Running {summary if len(summary) <= 60 else summary[:57] + '...'}")
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

    async def _verify(self, anchor, claim, actor="Verifier"):
        self.on_tool(f"Verifying {claim}")
        leaf = self.tree.append({
            "role": "user",
            "content": (
                "Use read_file or grep_search to check this claim. Call the tool now so you can see its output.\n"
                f"Claim to check: {claim}"
            ),
        }, parent_id=anchor, branch_type="verification")
        start = leaf
        reason = "Verification reached its three-request limit."
        try:
            for attempt in range(3):
                message = await self._complete(
                    self._messages(leaf, system_prompt=_VERIFY_PROMPT), _READ_ONLY_TOOLS, fork=True, branch=start,
                    actor=actor,
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
                    messages, [], fork=True, response_format=_VERDICT_SCHEMA, branch=start, actor=actor,
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
        """Run one reviewer in a read-only fork and return its structured answer, or None without an idea."""
        self.on_tool(f"Reviewing as {role.name}: {role.focus}")
        self._actor(role.name, "looking at the stall", "work")
        request = (f"User's request: {self._user_request(anchor)[:1500]}\nReflection role: {role.name}\n"
                   f"Review reason: {stall}\n\n{role.focus}")
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
            if role.tools:
                # One request to gather evidence. Requiring a call keeps a small model from proposing a check
                # instead of running it. A reply in text still leads to the structured answer, which needs facts.
                message = await self._complete(context(leaf, REFLECTION_PROMPT), _READ_ONLY_TOOLS, fork=True,
                                               temperature=role.temperature, tool_choice="required", branch=start,
                                               actor=role.name)
                calls = _tool_calls(message)
                leaf = self.tree.append(message, parent_id=leaf, branch_type="reflection")
                if calls:
                    self._actor(role.name, f"checking {_short(_call_summary(calls[0]['function']))}", "work")
                    leaf, _ = await self._observe(leaf, calls, branch_type="reflection")
                self._actor(role.name, "writing its idea", "work")
                leaf = self.tree.append({
                    "role": "user",
                    "content": "Give your answer now using the evidence already in the conversation.",
                }, parent_id=leaf, branch_type="reflection")
            message = await self._complete(
                context(leaf, _PROPOSAL_PROMPT), [], fork=True,
                response_format=_proposal_schema(role.fields), temperature=role.temperature, branch=start,
                actor=role.name,
            )
            if _tool_calls(message):
                raise ServerError("The reviewer called a tool on its final request.")
            leaf = self.tree.append(message, parent_id=leaf, branch_type="reflection")
            try:
                answer = json.loads(message.get("content") or "")
                proposal = " ".join(str(answer["proposal"]).split())
            except (AttributeError, KeyError, TypeError, ValueError):
                answer, proposal = None, ""
            if proposal.strip(" ."):
                self.tree.finish_fork(leaf, anchor, "merged")
                self._actor(role.name, f"idea: {_short(proposal)}", "work")
                return answer
        except (ServerError, TurnLimitError) as error:
            reason = str(error)

        self.tree.finish_fork(leaf, anchor, "pruned")
        self.on_tool(f"Reflection {role.name}: {reason}")
        self._actor(role.name, "no idea", "quiet")
        return None

    async def _proven_idea(self, anchor, stall, role, brief, history):
        """Return a reviewer's idea for the main branch if a verifier confirms the facts it rests on."""
        answer = await self._review(anchor, stall, role, brief, history)
        self._tick("reviewers")
        if answer is None:
            return None
        facts = " ".join(str(answer.get("facts", "")).split())
        if facts.strip(" .").lower() in ("", "none", "unverified", "unknown"):
            self.on_tool(f"Reflection {role.name}: dropped, the idea names no facts.")
            self._actor(role.name, "dropped, no facts named", "quiet")
            return None
        self._actor(role.name, "verifier checks its facts", "work")
        self._tick("verifiers", done=0, total=1)
        verdict = await self._verify(anchor, f"These facts hold in the workspace: {facts}", actor=role.name)
        self._tick("verifiers")
        label, _, detail = verdict.partition("] ")
        if label != "[VERIFIED":
            why = "refuted" if label == "[FALSE ASSUMPTION" else "not proven"
            self.on_tool(f"Reflection {role.name}: dropped, its facts are {why}. {detail}")
            self._actor(role.name, f"dropped, facts {why}", "bad" if why == "refuted" else "quiet")
            return None
        self._actor(role.name, f"proven: {_short(answer.get('proposal', ''))}", "good")
        return f"{_format_proposal(answer)[:800]} Verifier: {detail}"

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
        start = time.monotonic()
        self._progress(phase="Reflection", reviewers=(0, len(roles)), **({} if manual else {"moderator": "waiting"}))
        for role in roles:
            self._actor(role.name, "waiting for a slot", "wait")
        if not manual:
            self._actor("Moderator", "waiting for proven ideas", "wait")
        # The reviewers, the verifiers of their facts, and the moderator have a budget of their own, as for the
        # plan challenge.
        self.batch_left, self.batch_name = REFLECTION_REQUESTS, "reflection"
        try:
            ideas = await asyncio.gather(*(self._proven_idea(anchor, reason, role, brief, history)
                                           for role in roles))
            found = [(role, idea) for role, idea in zip(roles, ideas) if idea]
            # Grounded roles come first, since their ideas start from the stall's own evidence.
            found.sort(key=lambda item: item[0].group != "grounded")
            choice = None
            if found and not manual:
                choice = await self._moderate(anchor, CHOICE_PROMPT,
                                              case(self._user_request(anchor), "",
                                                   [f"{role.name}: {idea}" for role, idea in found]),
                                              choice_schema([role.name for role, _ in found]))
        finally:
            self.batch_left = self.batch_name = None
        summary = ("Proven ideas\n" + "\n".join(f"- {role.name}: {idea}" for role, idea in found) if found
                   else "No reviewer idea rested on facts that a verifier could confirm.")
        self.on_tool(f"Strategy\nReflection proposals\n{summary}")
        self._strategy_event("### Proposals\n\n" + summary)
        self.track_strategy = True
        chosen = choice.get("role") if isinstance(choice, dict) else None
        ideas = dict((role.name, idea) for role, idea in found)
        if chosen in ideas:
            why = " ".join(str(choice.get("reason", "")).split())
            step = " ".join(str(choice.get("first_step", "")).split())
            plan = f"The moderator chose the {chosen} idea. Reason: {why} First step: {step}"
            self.on_tool(f"Moderator\n{plan}")
            self._strategy_event(f"### Plan\n\n{plan}")
            outcome = f"the moderator chose {chosen}: {step}"
            content = (f"Reflection results\n{heading}: {reason}\n\nChosen idea\n- {chosen}: {ideas[chosen]}\n\n"
                       f"{plan}\n\n{_AFTER_REFLECTION}\n\n{_CARRY_OUT}")
        elif chosen == "none":
            outcome = "the moderator found that none serves your request"
            self.on_tool("Moderator\nNo proven idea serves the user's request.")
            content = (f"Reflection results\n{heading}: {reason}\n\nThe moderator found that no proven idea serves "
                       f"the user's request. Do not go back to the edits or commands you repeated before this "
                       f"reflection.")
        else:
            # A manual reflection, no proven idea, or no usable choice: the main branch sees what was proven.
            outcome = "the main branch sees them all" if found else "nothing to try"
            content = f"Reflection results\n{heading}: {reason}\n\n{summary}\n\n{_AFTER_REFLECTION}"
        self.tree.append({"role": "user", "content": content})
        self.on_tool(f"Reflection: {len(found)} of {len(roles)} ideas proven; {outcome} ({_elapsed(start)})")

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

    def _first_change(self, calls):
        """Whether the calls make the task's first change to a project file, which starts a challenge."""
        if not self.challenge or self.challenged:
            return False
        small = []
        for call in calls:
            name = call["function"]["name"]
            if name not in EDIT_TOOLS:
                continue
            try:
                args = json.loads(call["function"]["arguments"])
                path = args["path"]
            except (KeyError, TypeError, ValueError):
                continue
            # Skills and strategy notes under .refl/, and scratch files in other hidden folders, record or support
            # the work rather than change the project.
            if not isinstance(path, str) or not path.strip() or _scratch(path):
                continue
            if not small_edit(name, args, self._replaced_text(path) if name == "write_file" else None):
                return True
            small.append(_call_summary(call["function"]))
        # A small edit does not use up the challenge, so the task's first larger change still gets one.
        if small:
            self.on_tool(f"Challenge\nSkipped for a small edit: {'; '.join(small)}")
        return False

    def _replaced_text(self, path):
        """The text that write_file would overwrite at `path`, or None for a new file.

        A large, irregular, or unreadable file gives a text that never counts as small.
        """
        try:
            target = self.workspace._path(path)
            if not target.exists():
                return None
            if target.is_file() and target.stat().st_size <= SMALL_EDIT_CHARS * 4:
                return target.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
        # Too large, not a regular file, or unreadable: never small.
        return "\n" * (SMALL_EDIT_LINES + 1)

    async def _challenge(self, calls):
        """Have critics argue why the plan fails, prove their objections, and make the main branch answer them.

        The requests come from a budget of their own, so the task keeps all of its --max-turns.
        """
        self.challenged = True
        self.batch_left, self.batch_name = CHALLENGE_REQUESTS, "plan challenge"
        self.challenge_outcome = "stopped early"
        start = time.monotonic()
        self._progress(phase="Plan check", critics=(0, len(CRITICS) + 1), moderator="waiting")
        for critic in (*CRITICS, SIMPLIFIER):
            self._actor(critic.name, "waiting for a slot", "wait")
        self._actor("Moderator", "waiting for proven findings", "wait")
        try:
            return await self._run_challenge(calls)
        finally:
            self.batch_left = self.batch_name = None
            self.on_tool(f"Plan check: {self.challenge_outcome} ({_elapsed(start)})")

    async def _run_challenge(self, calls):
        anchor = self.tree.head
        pending = "; ".join(_call_summary(call["function"]) for call in calls
                            if call["function"]["name"] in EDIT_TOOLS)
        self.on_tool(f"Challenge\nLooking for reasons the plan fails, and for a simpler one, before its first "
                     f"change: {pending}")
        # Each finding goes to a verifier as soon as it is ready. The simplifier runs alongside the critics.
        *results, simpler = await asyncio.gather(*(self._object(anchor, critic, pending) for critic in CRITICS),
                                                 self._simplify(anchor, pending))
        # A proven finding is (critic, the point it makes, its line with its confirmed facts).
        proven = []
        for critic, result in zip(CRITICS, results):
            if result is None:
                continue
            answer, verdict = result
            line = f"{critic.name}: {format_objection(answer)[:800]}"
            label, _, detail = verdict.partition("] ")
            if label == "[VERIFIED":
                proven.append((critic, objection_of(answer), f"{line} Verifier: {detail}"))
            else:
                # Only proven facts may argue against the plan, so an unproven objection is dropped too.
                why = "refuted" if label == "[FALSE ASSUMPTION" else "not proven"
                self.on_tool(f"Challenge\nDropped the {critic.name} objection, {why}: {detail}")
        findings = proven + ([simpler] if simpler is not None else [])
        if not findings:
            self.challenge_outcome = "no objection or simpler strategy held up"
            self.on_tool("Challenge\nNo objection or simpler strategy held up.")
            self.tree.append({"role": "user", "content": (
                f"Plan challenge\nNo objection to the plan held up in the workspace, and no simpler strategy was "
                f"proven. {_AFTER_CHALLENGE}")})
            return None

        # The moderator rules on each finding from its confirmed facts and the user's request alone.
        request = self._user_request(anchor)
        answer = await self._moderate(anchor, RULING_PROMPT, case(request, pending, [line for *_, line in findings]),
                                      ruling_schema(len(findings)))
        rulings = rulings_of(answer, len(findings))
        if rulings is None:
            self.on_tool("Moderator\nNo usable rulings, so every proven finding stands.")
            rulings = [("follow", "")] * len(findings)
        ruled = [(ruling, f"- {line}" + (f" Moderator: {reason}" if reason else ""))
                 for (*_, line), (ruling, reason) in zip(findings, rulings)]
        self.on_tool("Moderator\n" + "\n".join(f"{ruling}: {line[2:]}" for ruling, line in ruled))
        tally = [f"{sum(ruling == kind for ruling, _ in ruled)} {kind}" for kind in ("follow", "dismiss", "ask_user")]
        self._actor("Moderator", " · ".join(count for count in tally if not count.startswith("0 ")), "good")
        ask = [line for ruling, line in ruled if ruling == "ask_user"]
        follow = [line for ruling, line in ruled if ruling == "follow"]
        counted = f"{len(findings)} proven, {len(follow)} to follow, {len(findings) - len(follow) - len(ask)} dismissed"
        if ask:
            self.challenge_outcome = f"{len(findings)} proven; the moderator leaves the decision to you"
            return await self._ask_user("\n".join(ask))
        self.challenge_outcome = counted
        if not follow:
            self.on_tool("Moderator\nNo proven finding calls for a change of plan.")
            self.tree.append({"role": "user", "content": (
                f"Plan challenge\nThe moderator ruled that no proven finding calls for a change of your plan. "
                f"{_AFTER_CHALLENGE}")})
            return None
        followed = "\n".join(follow)
        self.tree.append({"role": "user", "content": (
            f"Plan challenge\nBefore your first change ({pending}), critics looked for reasons that your plan fails "
            f"and for a simpler strategy at the level of the goal. A verifier confirmed their facts, and the "
            f"moderator ruled on them against the user's request.\n\nFollow these findings\n{followed}\n\n"
            f"{_FOLLOW_REQUEST}")})
        await self._answer_objections()

        # The moderator checks the revised plan against its rulings, and the main branch gets one more try.
        for attempt in range(2):
            revised = self.tree.nodes[self.tree.head].message.get("content") or ""
            check = await self._moderate(
                self.tree.head, CHECK_PROMPT,
                f"{case(request, pending, [line[2:] for line in follow])}\n\nThe main branch's revised plan\n{revised}",
                CHECK_SCHEMA)
            status = check.get("status") if isinstance(check, dict) else None
            reason = " ".join(str(check.get("reason", "")).split()) if isinstance(check, dict) else ""
            if status != "not_complied":
                self.on_tool(f"Moderator\nThe revised plan follows the rulings. {reason}".rstrip())
                self.challenge_outcome = f"{counted}; the revised plan follows them"
                self._actor("Moderator", "the revised plan follows the rulings", "good")
                break
            self.on_tool(f"Moderator\nThe revised plan does not follow the rulings yet. {reason}".rstrip())
            if attempt:
                self.challenge_outcome = f"{counted}; the plan missed them twice, so you decide"
                return await self._ask_user(f"{followed}\nThe moderator: the revised plan still does not follow "
                                            f"these. {reason}")
            self.tree.append({"role": "user", "content": (
                f"Not followed yet\n{_NOT_FOLLOWED_REQUEST.format(reason=reason or 'no reason given.')}")})
            await self._answer_objections()
        self.tree.append({"role": "user", "content": _AFTER_CHALLENGE})
        return None

    async def _ask_user(self, standing):
        """Return a question for the user about the findings that only the user can settle, instead of the change."""
        self.on_tool(f"Moderator\nOnly you can settle this, so Reflect asks you to specify the request.\n{standing}")
        self.tree.append({"role": "user", "content": (
            f"For the user\nThe moderator ruled that only the user can settle these confirmed findings.\n\n"
            f"{standing}\n\n{_ASK_USER_REQUEST}")})
        self._progress(main="writing a question for you")
        self._actor("Main branch", "writing a question for you", "work")
        try:
            message = await self._complete(self._messages(self.tree.head), [])
            question = message.get("content")
            if _tool_calls(message) or not isinstance(question, str) or not question.strip():
                raise ServerError("Expected a question for the user.")
            question = question.strip()
        except (ServerError, TurnLimitError):
            question = (f"I have not made the change yet, because these confirmed findings need your decision:\n"
                        f"{standing}\n\nHow do you want the request specified?")
        self.tree.append({"role": "assistant", "content": question})
        return question

    async def _moderate(self, anchor, task, content, schema):
        """Ask the moderator one question in a fork that sees only `content`, and return its parsed answer."""
        start = leaf = self.tree.append({"role": "user", "content": content}, parent_id=anchor,
                                        branch_type="moderator")
        messages = [{"role": "system", "content": f"{MODERATOR_PROMPT}\n\n{task}"}, {"role": "user", "content": content}]
        doing = {RULING_PROMPT: "ruling", CHECK_PROMPT: "checking the revised plan", CHOICE_PROMPT: "choosing"}
        self._progress(moderator=doing.get(task, "deciding"))
        self._actor("Moderator", doing.get(task, "deciding"), "work")
        try:
            message = await self._complete(messages, [], fork=True, response_format=schema, branch=start,
                                           actor="Moderator")
            leaf = self.tree.append(message, parent_id=leaf, branch_type="moderator")
            answer = json.loads(message.get("content") or "")
        except (ServerError, TurnLimitError, TypeError, ValueError) as error:
            self.tree.finish_fork(leaf, anchor, "pruned")
            self.on_tool(f"Moderator\nNo answer: {error}")
            self._progress(moderator="no answer")
            self._actor("Moderator", "no answer", "bad")
            return None
        self.tree.finish_fork(leaf, anchor, "merged")
        self._progress(moderator="done")
        self._actor("Moderator", "decided", "good")
        return answer

    async def _answer_objections(self):
        """Have the main branch answer the findings in the last message, without tools."""
        self._progress(main="revising its plan")
        self._actor("Main branch", "revising its plan", "work")
        try:
            message = await self._complete(self._messages(self.tree.head), [])
            answer = message.get("content")
            if _tool_calls(message) or not isinstance(answer, str) or not answer.strip():
                raise ServerError("Expected a text answer to the objections.")
        except (ServerError, TurnLimitError) as error:
            self.on_tool(f"Challenge\nNo answer to the objections: {error}")
            answer = "I will take the objections into account."
        else:
            answer = answer.strip()
            self.on_tool(f"Challenge\nAnswer\n{answer}")
        self._progress(main=None)
        self._actor("Main branch", "plan revised", "good")
        self.tree.append({"role": "assistant", "content": answer})

    async def _object(self, anchor, critic, pending):
        """Return a critic's answer and the verifier's verdict on its objection, or None without one."""
        answer = await self._critique(anchor, critic, pending)
        self._tick("critics")
        if answer is None:
            return None
        self._tick("verifiers", done=0, total=1)
        verdict = await self._verify(anchor, f"The planned change ({pending}) fails: {objection_of(answer)}",
                                     actor=critic.name)
        self._tick("verifiers")
        label = verdict[1:].partition("]")[0]
        self._actor(critic.name, {"VERIFIED": "objection proven", "FALSE ASSUMPTION": "objection refuted"}.get(
            label, "objection not proven"), {"VERIFIED": "good", "FALSE ASSUMPTION": "bad"}.get(label, "quiet"))
        return answer, verdict

    async def _simplify(self, anchor, pending):
        """Return the simplifier's finding if a verifier proves that its strategy keeps the requirements."""
        answer = await self._critique(anchor, SIMPLIFIER, pending, prompt=SIMPLIFY_PROMPT,
                                      answer_prompt=SIMPLIFY_ANSWER_PROMPT, schema=simplify_schema(),
                                      found=simplification_of, nothing="The current plan is already about as simple.")
        self._tick("critics")
        if answer is None:
            return None
        strategy = simplification_of(answer)
        self._tick("verifiers", done=0, total=1)
        verdict = await self._verify(anchor, simplification_claim(answer), actor=SIMPLIFIER.name)
        self._tick("verifiers")
        label, _, detail = verdict.partition("] ")
        if label != "[VERIFIED":
            self.on_tool(f"Challenge\nDropped the simpler strategy: {detail}")
            self._actor(SIMPLIFIER.name, "strategy not proven", "bad" if label == "[FALSE ASSUMPTION" else "quiet")
            return None
        self._actor(SIMPLIFIER.name, "simpler strategy proven", "good")
        line = f"{SIMPLIFIER.name}: {format_simplification(answer)[:800]} Verifier: {detail}"
        return SIMPLIFIER, strategy, line

    async def _critique(self, anchor, critic, pending, *, prompt=CHALLENGE_PROMPT, answer_prompt=ANSWER_PROMPT,
                        schema=None, found=objection_of, nothing="The critic returned no objection."):
        """Run one critic in a read-only fork and return its structured answer, or None if `found` sees nothing."""
        self.on_tool(f"Challenging as {critic.name}")
        self._actor(critic.name, "looking at the plan", "work")
        start = leaf = self.tree.append({
            "role": "user",
            "content": (f"User's request: {self._user_request(anchor)[:1500]}\nCritic role: {critic.name}\n"
                        f"Planned first change: {pending}\n\n{critic.focus}"),
        }, parent_id=anchor, branch_type="challenge")
        role = f"Your role: {critic.name}. {critic.focus}"
        reason = nothing
        try:
            # One request to gather evidence, then the answer without tools, as for a reflection reviewer.
            message = await self._complete(self._messages(leaf, system_prompt=f"{prompt}\n\n{role}"),
                                           _READ_ONLY_TOOLS, fork=True, temperature=critic.temperature,
                                           tool_choice="required", branch=start, actor=critic.name)
            calls = _tool_calls(message)
            leaf = self.tree.append(message, parent_id=leaf, branch_type="challenge")
            if calls:
                self._actor(critic.name, f"checking {_short(_call_summary(calls[0]['function']))}", "work")
                leaf, _ = await self._observe(leaf, calls, branch_type="challenge")
            self._actor(critic.name, "writing its finding", "work")
            leaf = self.tree.append({
                "role": "user",
                "content": "Give your answer now using the evidence already in the conversation.",
            }, parent_id=leaf, branch_type="challenge")
            message = await self._complete(
                self._messages(leaf, system_prompt=f"{prompt} {answer_prompt}\n\n{role}"), [], fork=True,
                response_format=schema or answer_schema(), temperature=critic.temperature, branch=start,
                actor=critic.name,
            )
            if _tool_calls(message):
                raise ServerError("The critic called a tool on its final request.")
            leaf = self.tree.append(message, parent_id=leaf, branch_type="challenge")
            try:
                answer = json.loads(message.get("content") or "")
            except (TypeError, ValueError):
                answer = None
            if found(answer):
                self.tree.finish_fork(leaf, anchor, "merged")
                self._actor(critic.name, f"{_short(found(answer))}; verifier checks it", "work")
                return answer
        except (ServerError, TurnLimitError) as error:
            reason = str(error)

        self.tree.finish_fork(leaf, anchor, "pruned")
        self.on_tool(f"Challenge {critic.name}: {reason}")
        self._actor(critic.name, "nothing found" if reason == nothing else _short(reason), "quiet")
        return None

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
        self.challenged = False
        self.wrote_files = False
        self._close_unfinished_round()
        self.tree.append({"role": "user", "content": prompt})
        self.tree.save()
        self.requests_left = self.max_turns
        try:
            answer = await self._respond()
            self._keep_report(prompt, answer)
            return answer
        except (ServerError, TurnLimitError) as error:
            note = f"[Turn stopped] {error}"
            # The report and the note form one reply, so the conversation keeps alternating between roles.
            report = getattr(error, "report", None)
            if report:
                self._keep_report(prompt, f"{report}\n\n{note}")
            self.tree.append({"role": "assistant", "content": f"{report}\n\n{note}" if report else note})
            self.tree.save()
            raise
        finally:
            self.track_strategy = False
            self.requests_left = None

    def _keep_report(self, prompt, answer):
        """Keep the turn's answer in .refl/sessions/last-report.md, so that a later session can pick up from it.

        A model asked to "resume" in a new session spent eight requests reading raw session logs instead.
        """
        path = self.tree.path.parent / _LAST_REPORT
        try:
            path.write_text(f"# Last report\n\nSession: {self.tree.path.stem}\n\nRequest: {prompt}\n\n{answer}\n",
                            encoding="utf-8")
        except OSError as error:
            self.on_tool(f"Last report unavailable: {error}")

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
        # The smoke check took the last request, so without a report the user saw only its exit code. The report
        # comes on top of the limit, as when a turn runs out of requests.
        report = await self._limit_report()
        if report is None:
            return summary
        self.tree.append({"role": "assistant", "content": report})
        self.tree.save()
        return f"{report}\n\n{summary}"

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
            if calls and answer.strip():
                # The words to the user read as the conversation, with later lines under the first.
                words = _VERIFY_TAG.sub(lambda match: match[1], answer).strip()
                self.on_tool(CHAT_MARK + words.replace("\n", "\n" + " " * len(CHAT_MARK)))

            # The challenge has its own requests, but the plan needs at least one more of the turn's.
            challenge = not claims and self.requests_left >= 1 and self._first_change(calls)
            leaf = self.tree.append(message)
            defer = "the verification verdict" if claims else "the plan challenge" if challenge else None
            await self._observe(leaf, calls, defer=defer, stalls=self.stalls if self.forks else None)
            if challenge:
                self.tree.save()
                question = await self._challenge(calls)
                self.tree.save()
                if question:
                    # The deferred change stays unmade until the user has specified the request.
                    return question
                continue
            if claims:
                anchor = self.tree.head
                self._progress(phase="Checking claims", verifiers=(0, len(claims)))

                async def checked(number, claim):
                    name = f"Verifier {number}"
                    self._actor(name, f"checking: {_short(claim)}", "work")
                    verdict = await self._verify(anchor, claim, actor=name)
                    self._tick("verifiers")
                    label = verdict[1:].partition("]")[0]
                    self._actor(name, f"{label.lower()}: {_short(claim)}",
                                {"VERIFIED": "good", "FALSE ASSUMPTION": "bad"}.get(label, "quiet"))
                    return verdict

                verdicts = await asyncio.gather(*(checked(number, claim) for number, claim in enumerate(claims, 1)))
                self.verdicts.update(zip(claims, verdicts))
                findings = [f"{claim}\n{verdict}" for claim, verdict in zip(claims, verdicts)]
                for claim, verdict in zip(claims, verdicts):
                    self.on_tool(verdict)
                    self.on_tool(f"Check: {verdict[1:].partition(']')[0].lower()}: {claim}")
                self.tree.append({
                    "role": "user",
                    "content": "Verification results\n\n" + "\n\n".join(findings) + (
                        "\n\nContinue the original task using these findings. Reuse the verdicts for these "
                        "claims. Call the tools for any remaining work, or answer if the task is complete. "
                        "Reissue any deferred calls that are still needed."
                    ),
                })
            elif (self.forks and self.requests_left >= _ACT_ON_REFLECTION
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
