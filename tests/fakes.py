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

    def __init__(self, replies, *, moves_text=False):
        self.replies = list(replies)
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
                       thinking=True):
        self.requests.append({
            "system": messages[0]["content"],
            "messages": messages,
            "tools": [tool["function"]["name"] for tool in tools],
            "response_format": response_format,
            "temperature": temperature,
            "thinking": thinking,
        })
        if not self.replies:
            raise AssertionError("Unexpected model request")
        return self.replies.pop(0)


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


def make_engine(root, replies, *, max_turns=20, forks=True, verification=True, thinking="auto", moves_text=False):
    events = []
    tree = ConversationTree(Path(root) / ".refl" / "sessions", SYSTEM_PROMPT)
    server = FakeServer(replies, moves_text=moves_text)
    engine = Engine(server, "model", WorkspaceTools(root), tree, max_turns, 256, events.append,
                    forks=forks, verification=verification, thinking=thinking)
    return engine, server, events
