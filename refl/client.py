import httpx


class ServerError(RuntimeError):
    """An unavailable llama-server or an invalid completion response."""


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

    async def slots(self):
        slots = await self._request("GET", "/slots", timeout=5)
        if not isinstance(slots, list) or not all(
            isinstance(slot, dict) and isinstance(slot.get("is_processing"), bool) for slot in slots
        ):
            raise ServerError("Unexpected /slots response")
        return len(slots), sum(slot["is_processing"] for slot in slots)

    async def complete(self, model, messages, tools, max_tokens, *, response_format=None, temperature=0.0):
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
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
        return message
