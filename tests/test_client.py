import asyncio
import json
import unittest

import httpx

from refl.client import THINKING_BUDGET_MESSAGE, ContextLimitError, LlamaClient


def serve(handler):
    """Run a coroutine factory against a client whose requests go to `handler`."""
    async def run(use):
        async with httpx.AsyncClient(base_url="http://server", transport=httpx.MockTransport(handler)) as http:
            return await use(LlamaClient(http))
    return run


class ClientTest(unittest.TestCase):
    def test_properties_report_the_context_per_slot_and_the_build(self):
        seen = []

        def handler(request):
            seen.append(request.url.params.get("model"))
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 32768},
                                             "build_info": "b12560-6aee50801"})

        result = asyncio.run(serve(handler)(lambda client: client.properties("qwen")))
        self.assertEqual(result, (32768, "b12560-6aee50801"))
        self.assertEqual(seen, ["qwen"])

    def test_a_router_without_the_model_reports_no_context_size(self):
        def handler(request):
            return httpx.Response(200, json={"role": "router", "default_generation_settings": {"n_ctx": 0}})

        self.assertEqual(asyncio.run(serve(handler)(lambda client: client.properties("qwen"))), (None, None))

    def test_completion_returns_the_context_tokens(self):
        def handler(request):
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Hi."}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 2, "total_tokens": 42},
            })

        message, tokens = asyncio.run(serve(handler)(lambda client: client.complete("qwen", [], [], 16)))
        self.assertEqual((message["content"], tokens), ("Hi.", 42))

    def test_a_request_that_does_not_fit_raises_a_context_error(self):
        def handler(request):
            return httpx.Response(400, json={"error": {
                "code": 400, "type": "exceed_context_size_error", "n_prompt_tokens": 33010, "n_ctx": 32768,
                "message": "request (33010 tokens) exceeds the available context size (32768 tokens)",
            }})

        with self.assertRaises(ContextLimitError) as raised:
            asyncio.run(serve(handler)(lambda client: client.complete("qwen", [], [], 16)))
        self.assertEqual((raised.exception.tokens, raised.exception.n_ctx), (33010, 32768))
        self.assertEqual(str(raised.exception),
                         "The request needs 33,010 tokens, but llama-server holds 32,768 tokens of context per slot.")


    def test_a_thinking_budget_is_sent_only_with_thinking(self):
        payloads = []

        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Hi."}}]})

        async def use(client):
            await client.complete("qwen", [], [], 2048, thinking_budget=1024)
            await client.complete("qwen", [], [], 2048, thinking=False, thinking_budget=1024)
            await client.complete("qwen", [], [], 2048)

        asyncio.run(serve(handler)(use))
        self.assertEqual(payloads[0]["reasoning_budget_tokens"], 1024)
        self.assertEqual(payloads[0]["reasoning_budget_message"], f"\n\n{THINKING_BUDGET_MESSAGE}\n")
        self.assertFalse({"reasoning_budget_tokens", "reasoning_budget_message"} & (set(payloads[1]) | set(payloads[2])))

if __name__ == "__main__":
    unittest.main()
