import re
from dataclasses import dataclass

from .transport import ReplyTimeout, TransportError


# Text that llama-server writes at the end of a thinking block that used up its budget.
THINKING_BUDGET_MESSAGE = "Thinking budget reached. I will act on the best plan so far."

# Basalt's reply to a request that does not fit. Unlike llama-server, it counts max_tokens against the context.
_BASALT_CONTEXT_ERROR = re.compile(
    r"prompt \((\d+) tokens\) \+ max tokens \((\d+)\) exceeds the context \((\d+)\)")


class ServerError(RuntimeError):
    """An unavailable llama-server or an invalid completion response."""


class TokenLimitError(ServerError):
    """A reply that reached the output token limit before it was complete."""


class ContextLimitError(ServerError):
    """A request that does not fit in the context of a llama-server slot."""

    def __init__(self, tokens, n_ctx, advice="", *, reply_tokens=None):
        if reply_tokens is None:
            message = (f"The request needs {tokens:,} tokens, but llama-server holds {n_ctx:,} tokens of context "
                       f"per slot.")
        else:
            message = (f"The request needs {tokens:,} tokens plus {reply_tokens:,} for the reply, but the server "
                       f"holds {n_ctx:,} tokens of context per slot. This server counts --max-tokens against the "
                       f"context, so a lower --max-tokens leaves more room for the conversation.")
        super().__init__(f"{message} {advice}".strip())
        self.tokens = tokens
        self.n_ctx = n_ctx
        # The reply tokens that the server counted against the context, or None if it counts the prompt only.
        self.reply_tokens = reply_tokens


@dataclass
class ServerProperties:
    """What /props reports about the server and its chat template. None means the server did not say."""

    context_size: int = None
    build: str = None
    # Reasoning effort values that the template renders distinctly, lowest first, and the values it raises an
    # error for.
    effort_levels: list = None
    rejected_efforts: list = None
    # Whether the server takes images, which needs a vision model and its --mmproj file.
    vision: bool = None


def _pieces(event):
    """The (kind, text) pieces of one streamed chat completion event."""
    for choice in event.get("choices") or []:
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if not isinstance(delta, dict):
            continue
        for field, kind in (("reasoning_content", "thinking"), ("content", "writing")):
            if isinstance(delta.get(field), str) and delta[field]:
                yield kind, delta[field]
        for call in delta.get("tool_calls") or []:
            arguments = (call.get("function") or {}).get("arguments") if isinstance(call, dict) else None
            if isinstance(arguments, str) and arguments:
                yield "tool", arguments


def _assemble(events):
    """Join the events of a streamed reply into the response that the same request without streaming gets."""
    content, reasoning, calls, finish, usage = [], [], {}, None, None
    for event in events:
        if not isinstance(event, dict):
            continue
        usage = event.get("usage") or usage
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning.append(delta["reasoning_content"])
            for part in delta.get("tool_calls") or []:
                call = calls.setdefault(part.get("index", len(calls)),
                                        {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                call["id"] = part.get("id") or call["id"]
                function = part.get("function") or {}
                call["function"]["name"] += function.get("name") or ""
                call["function"]["arguments"] += function.get("arguments") or ""
    message = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    data = {"choices": [{"message": message, "finish_reason": finish}]}
    if usage is not None:
        data["usage"] = usage
    return data


def _strings(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else None


class LlamaClient:
    def __init__(self, http):
        self.http = http

    async def _request(self, method, path, **kwargs):
        try:
            response = await self.http.request(method, path, **kwargs)
        except ReplyTimeout as error:
            raise ServerError(f"llama-server did not finish its reply within {error.seconds} s. Raise --timeout, "
                              f"or lower --max-tokens.") from error
        except TransportError as error:
            raise ServerError(f"Could not reach llama-server at {self.http.base_url}: {error}") from error
        if response.events is not None:
            return _assemble(response.events)
        try:
            data = response.json()
        except ValueError:
            data = None
        if not response.is_success:
            detail = data.get("error") if isinstance(data, dict) else None
            if isinstance(detail, dict):
                tokens, n_ctx = detail.get("n_prompt_tokens"), detail.get("n_ctx")
                if (detail.get("type") == "exceed_context_size_error" and isinstance(tokens, int)
                        and isinstance(n_ctx, int)):
                    raise ContextLimitError(tokens, n_ctx)
                detail = detail.get("message")
                match = _BASALT_CONTEXT_ERROR.search(detail) if isinstance(detail, str) else None
                if match:
                    tokens, reply_tokens, n_ctx = map(int, match.groups())
                    raise ContextLimitError(tokens, n_ctx, reply_tokens=reply_tokens)
            raise ServerError(f"{path} returned HTTP {response.status}: {detail or response.reason}")
        return data

    async def check_health(self):
        data = await self._request("GET", "/health", timeout=5)
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise ServerError("Unexpected /health response. Check the llama-server URL.")

    async def default_model(self):
        data = await self._request("GET", "/v1/models", timeout=5)
        try:
            model = data["data"][0]["id"]
        except (KeyError, IndexError, TypeError) as error:
            raise ServerError("No model found at /v1/models. Load a GGUF or pass --model.") from error
        if not isinstance(model, str) or not model:
            raise ServerError("Invalid model ID at /v1/models. Pass --model to select a model.")
        return model

    async def properties(self, model):
        """Return the context size per slot, the build, and the template's reasoning effort levels."""
        # A router serves several models and answers for one of them only when the request names it.
        data = await self._request("GET", "/props", params={"model": model}, timeout=5)
        if not isinstance(data, dict):
            raise ServerError("Unexpected /props response")
        settings = data.get("default_generation_settings")
        n_ctx = settings.get("n_ctx") if isinstance(settings, dict) else None
        build = data.get("build_info")
        caps = data.get("chat_template_caps")
        caps = caps if isinstance(caps, dict) else {}
        modalities = data.get("modalities")
        modalities = modalities if isinstance(modalities, dict) else {}
        return ServerProperties(
            context_size=n_ctx if isinstance(n_ctx, int) and n_ctx > 0 else None,
            build=build if isinstance(build, str) and build else None,
            effort_levels=_strings(caps.get("reasoning_effort_levels")),
            rejected_efforts=_strings(caps.get("reasoning_effort_rejected")),
            vision=vision if isinstance(vision := modalities.get("vision"), bool) else None,
        )

    async def slots(self):
        slots = await self._request("GET", "/slots", timeout=5)
        if not isinstance(slots, list) or not all(
            isinstance(slot, dict) and isinstance(slot.get("is_processing"), bool) for slot in slots
        ):
            raise ServerError("Unexpected /slots response")
        return len(slots), sum(slot["is_processing"] for slot in slots)

    async def slot_stats(self):
        """What /slots reports about each slot, such as whether it is busy and the tokens it generated."""
        slots = await self._request("GET", "/slots", timeout=5)
        if not isinstance(slots, list):
            raise ServerError("Unexpected /slots response")
        return slots

    async def apply_template(self, messages, tools):
        """Render messages with the server's chat template without running the model."""
        data = await self._request("POST", "/apply-template", json={"messages": messages, "tools": tools},
                                   timeout=30)
        prompt = data.get("prompt") if isinstance(data, dict) else None
        if not isinstance(prompt, str):
            raise ServerError("Unexpected /apply-template response")
        return prompt

    async def complete(self, model, messages, tools, max_tokens, *, response_format=None, temperature=0.0,
                       thinking=True, tool_choice=None, thinking_budget=None, reasoning_effort=None, session=None,
                       on_text=None):
        """Return the assistant message and the tokens the request used in the context, or None for those.

        With `on_text`, the reply streams, and each piece goes to on_text(kind, text) as it arrives: kind is
        thinking, writing, or tool. The returned message is the same as without streaming.
        """
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if not thinking:
            # Chat templates with a thinking mode, such as Qwen3's, read this flag. Others ignore it.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        else:
            if thinking_budget is not None:
                # The server ends the thinking block after this many tokens, so the reply still has room for an
                # answer. Builds without this feature ignore both fields.
                payload["reasoning_budget_tokens"] = thinking_budget
                payload["reasoning_budget_message"] = f"\n\n{THINKING_BUDGET_MESSAGE}\n"
            if reasoning_effort is not None:
                # The server passes the level to chat templates that read it, such as Qwen3.8's.
                payload["reasoning_effort"] = reasoning_effort
        # Servers such as Basalt keep a conversation on one slot by this header, so its cached context is reused.
        # llama-server ignores it and picks the slot whose cache matches the prompt best.
        headers = {"x-session-id": session} if session else None
        def relay(event):
            for kind, text in _pieces(event):
                on_text(kind, text)

        if on_text is not None:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        data = await self._request("POST", "/v1/chat/completions", json=payload, headers=headers,
                                   on_event=relay if on_text is not None else None)
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise ServerError("Invalid chat completion response") from error
        if choice.get("finish_reason") == "length":
            raise TokenLimitError("Completion reached its token limit. Increase --max-tokens.")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ServerError("Expected an assistant message in the chat completion")
        usage = data.get("usage")
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        return message, (tokens if isinstance(tokens, int) and tokens > 0 else None)
