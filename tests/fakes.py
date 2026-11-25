import itertools
import json
from pathlib import Path

from refl.client import ServerError
from refl.engine import SYSTEM_PROMPT, Engine
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
                       thinking=True, tool_choice=None, thinking_budget=None, reasoning_effort=None, session=None):
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
        })
        routed = self.route(self.requests[-1]) if self.route else None
        if routed is not None:
            if isinstance(routed, Exception):
                raise routed
            return routed, None
        if not self.replies:
            raise AssertionError("Unexpected model request")
        reply = self.replies.pop(0)
        tokens = self.tokens.pop(0) if self.tokens else None
        if isinstance(reply, Exception):
            raise reply
        return reply, tokens


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


def make_engine(root, replies, *, max_turns=20, forks=True, verification=True, thinking="auto", moves_text=False,
                tokens=None, context_size=None, thinking_budget=None, fork_effort=None, vision=False, challenge=False, route=None):
    """Without `challenge`, scripted replies need no answers for the critics."""
    events = []
    tree = ConversationTree(Path(root) / ".refl" / "sessions", SYSTEM_PROMPT)
    server = FakeServer(replies, moves_text=moves_text, tokens=tokens, route=route)
    engine = Engine(server, "model", WorkspaceTools(root), tree, max_turns, 256, events.append,
                    forks=forks, verification=verification, thinking=thinking, context_size=context_size,
                    thinking_budget=thinking_budget, fork_effort=fork_effort, vision=vision, challenge=challenge)
    return engine, server, events
