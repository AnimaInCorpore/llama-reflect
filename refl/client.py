import httpx


# Text that llama-server writes at the end of a thinking block that used up its budget.
THINKING_BUDGET_MESSAGE = "Thinking budget reached. I will act on the best plan so far."


class ServerError(RuntimeError):
    """An unavailable llama-server or an invalid completion response."""


class ContextLimitError(ServerError):
    """A request that does not fit in the context of a llama-server slot."""

    def __init__(self, tokens, n_ctx, advice=""):
        message = (f"The request needs {tokens:,} tokens, but llama-server holds {n_ctx:,} tokens of context "
                   f"per slot.")
        super().__init__(f"{message} {advice}".strip())
        self.tokens = tokens
        self.n_ctx = n_ctx


class LlamaClient:
    def __init__(self, http):
        self.http = http

    async def _request(self, method, path, **kwargs):
        try:
            response = await self.http.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise ServerError(f"Could not reach llama-server at {self.http.base_url}: {error}") from error
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
            raise ServerError(f"{path} returned HTTP {response.status_code}: {detail or response.reason_phrase}")
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
        """Return the context size per slot and the build from /props. Either is None when unknown."""
        # A router serves several models and answers for one of them only when the request names it.
        data = await self._request("GET", "/props", params={"model": model}, timeout=5)
        if not isinstance(data, dict):
            raise ServerError("Unexpected /props response")
        settings = data.get("default_generation_settings")
        n_ctx = settings.get("n_ctx") if isinstance(settings, dict) else None
        build = data.get("build_info")
        return (n_ctx if isinstance(n_ctx, int) and n_ctx > 0 else None,
                build if isinstance(build, str) and build else None)

    async def slots(self):
        slots = await self._request("GET", "/slots", timeout=5)
        if not isinstance(slots, list) or not all(
            isinstance(slot, dict) and isinstance(slot.get("is_processing"), bool) for slot in slots
        ):
            raise ServerError("Unexpected /slots response")
        return len(slots), sum(slot["is_processing"] for slot in slots)

    async def apply_template(self, messages, tools):
        """Render messages with the server's chat template without running the model."""
        data = await self._request("POST", "/apply-template", json={"messages": messages, "tools": tools},
                                   timeout=30)
        prompt = data.get("prompt") if isinstance(data, dict) else None
        if not isinstance(prompt, str):
            raise ServerError("Unexpected /apply-template response")
        return prompt

    async def complete(self, model, messages, tools, max_tokens, *, response_format=None, temperature=0.0,
                       thinking=True, tool_choice=None, thinking_budget=None):
        """Return the assistant message and the tokens the request used in the context, or None for those."""
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
        elif thinking_budget is not None:
            # The server ends the thinking block after this many tokens, so the reply still has room for an
            # answer. Builds without this feature ignore both fields.
            payload["reasoning_budget_tokens"] = thinking_budget
            payload["reasoning_budget_message"] = f"\n\n{THINKING_BUDGET_MESSAGE}\n"
        data = await self._request("POST", "/v1/chat/completions", json=payload)
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise ServerError("Invalid chat completion response") from error
        if choice.get("finish_reason") == "length":
            raise ServerError("Completion reached its token limit. Increase --max-tokens.")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ServerError("Expected an assistant message in the chat completion")
        usage = data.get("usage")
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        return message, (tokens if isinstance(tokens, int) and tokens > 0 else None)
