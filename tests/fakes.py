import itertools
import json
from pathlib import Path

from refl.client import ServerError
from refl.engine import SYSTEM_PROMPT, Engine
from refl.moderator import CHOICE_PROMPT, MODERATOR_PROMPT
from refl.reflection import REFLECTION_PROMPT
from refl.tools import WorkspaceTools
from refl.tree import ConversationTree

_ids = itertools.count()


class FakeServer:
    """Return scripted assistant messages in request order and record each request."""

    def __init__(self, replies, *, moves_text=False, tokens=None, route=None):
        # A reply that is an exception is raised instead. `tokens` lists the context tokens each request reports.
        # `route(request)` can answer a request by its content, such as a fork that runs concurrently with
        # others. When it returns None, the request takes the next scripted reply.
        self.replies = list(replies)
        self.route = route
        self.tokens = list(tokens or [])
        self.requests = []
        # True renders an assistant's text after its tool results, like Gemma 4's template. None has no
        # /apply-template endpoint.
        self.moves_text = moves_text
        self.template_requests = 0

    async def apply_template(self, messages, tools):
        self.template_requests += 1
        if self.moves_text is None:
            raise ServerError("/apply-template returned HTTP 404: Not Found")
        parts, after = [], []
        for message in messages:
            content = str(message.get("content") or "")
            if self.moves_text and message["role"] == "assistant" and message.get("tool_calls"):
                after.append(content)
            else:
                parts.append(content)
        return "\n".join(parts + after)

    async def complete(self, model, messages, tools, max_tokens, *, response_format=None, temperature=0.0,
                       thinking=True, tool_choice=None, thinking_budget=None, reasoning_effort=None, session=None,
                       on_text=None):
        self.requests.append({
            "system": messages[0]["content"],
            "messages": messages,
            "tools": [tool["function"]["name"] for tool in tools],
            "response_format": response_format,
            "temperature": temperature,
            "thinking": thinking,
            "tool_choice": tool_choice,
            "thinking_budget": thinking_budget,
            "reasoning_effort": reasoning_effort,
            "session": session,
            "streamed": on_text is not None,
        })
        routed = self.route(self.requests[-1]) if self.route else None
        if routed is not None:
            if isinstance(routed, Exception):
                raise routed
            _stream(routed, on_text)
            return routed, None
        if not self.replies:
            raise AssertionError("Unexpected model request")
        reply = self.replies.pop(0)
        tokens = self.tokens.pop(0) if self.tokens else None
        if isinstance(reply, Exception):
            raise reply
        _stream(reply, on_text)
        return reply, tokens


def _stream(reply, on_text):
    """Hand a scripted reply to on_text word by word, as a streaming server would piece by piece."""
    if on_text is None:
        return
    for field, kind in (("reasoning_content", "thinking"), ("content", "writing")):
        for word in str(reply.get(field) or "").split():
            on_text(kind, word + " ")
    for call in reply.get("tool_calls") or []:
        on_text("tool", call["function"]["arguments"])


def text(content):
    return {"role": "assistant", "content": content}


def call(name, **arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": f"call{next(_ids)}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


def verdict(status="supported", reason="status.txt line 1 contains old_ready"):
    return text(json.dumps({"reason": reason, "status": status}))


def decision(role="Diagnosis", reason="It explains the failures.", first_step="Run the check."):
    return text(json.dumps({"role": role, "reason": reason, "first_step": first_step}))


def reflection_route(ideas=None, statuses=None, status="supported", facts="a.txt line 1 says a", choose=None):
    """Answer reflection reviewers, the verifiers of their facts, and the moderator by content.

    `ideas` maps a role to its proposal, and other roles propose "<role> idea". `facts` is every idea's facts,
    or a dict from role to facts. The verdict is the status of the first word of `statuses` in the facts, or
    `status`. The moderator chooses the role `choose`, or the first idea it is shown, and answers in plain
    text for "invalid".
    """
    def route(request):
        system = request["system"]
        if system.startswith(MODERATOR_PROMPT) and CHOICE_PROMPT in system:
            if choose == "invalid":
                return text("not json")
            first = request["messages"][1]["content"].split("confirmed\n", 1)[1].split(". ", 1)[1].split(":")[0]
            return text(json.dumps({"reason": "a.txt line 1 explains the stall", "first_step": "Run the check.",
                                    "role": choose or first}))
        properties = ((request["response_format"] or {}).get("schema") or {}).get("properties", {})
        if system.startswith(REFLECTION_PROMPT) and request["tools"]:
            return text("Nothing to read.")
        if "proposal" in properties:
            role = system.split("Your role: ")[1].split(".")[0]
            answer = {"proposal": (ideas or {}).get(role, f"{role} idea"),
                      "facts": facts.get(role, "") if isinstance(facts, dict) else facts,
                      "basis": "it fits", "cost": "one command", "fails_if": "nothing changes"}
            if "abstraction" in properties:
                answer["abstraction"] = "parts and a goal"
            return text(json.dumps(answer))
        if system.startswith("You are Reflect's read-only verifier"):
            return call("grep_search", pattern=".", path=".")
        if "status" in properties and system.startswith("Compare the exact claim"):
            claim = request["messages"][1]["content"].split("\n\nCurrent evidence")[0]
            found = next((value for word, value in (statuses or {}).items() if word in claim), status)
            return verdict(found, f"{found} by the workspace")
        return None
    return route


def make_engine(root, replies, *, max_turns=20, forks=True, verification=True, thinking="auto", moves_text=False,
                tokens=None, context_size=None, thinking_budget=None, fork_effort=None, vision=False, challenge=False, route=None, fork_thinking_budget=None,
                on_progress=None):
    """Without `challenge`, scripted replies need no answers for the critics."""
    events = []
    tree = ConversationTree(Path(root) / ".refl" / "sessions", SYSTEM_PROMPT)
    server = FakeServer(replies, moves_text=moves_text, tokens=tokens, route=route)
    engine = Engine(server, "model", WorkspaceTools(root), tree, max_turns, 256, events.append,
                    forks=forks, verification=verification, thinking=thinking, context_size=context_size,
                    thinking_budget=thinking_budget, fork_effort=fork_effort, vision=vision, challenge=challenge,
                    fork_thinking_budget=fork_thinking_budget,
                    on_progress=on_progress)
    return engine, server, events
