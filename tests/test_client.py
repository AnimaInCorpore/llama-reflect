import asyncio
import json
import socket
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from refl.client import THINKING_BUDGET_MESSAGE, ContextLimitError, LlamaClient, ServerError, ServerProperties
from refl.transport import HttpClient

_REPLY = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Hi."}}]}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        url = urllib.parse.urlsplit(self.path)
        request = SimpleNamespace(method=self.command, path=url.path, params=dict(urllib.parse.parse_qsl(url.query)),
                                  json=json.loads(body) if body else None, headers=self.headers)
        status, payload = self.server.handler(request)
        data = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass  # The client stopped waiting.

    do_POST = do_GET

    def log_message(self, format, *args):
        pass


class LocalServer:
    """An HTTP server on a free local port that answers each request with handler(request)."""

    def __init__(self, handler):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.handler = handler
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        return self

    def __exit__(self, *exc_info):
        self.server.shutdown()
        self.server.server_close()


def serve(handler, use, **options):
    """Run use(client) with a LlamaClient whose requests go to handler."""
    with LocalServer(handler) as server:
        return asyncio.run(use(LlamaClient(HttpClient(server.url, **options))))


class ClientTest(unittest.TestCase):
    def test_properties_report_the_context_the_build_and_the_effort_levels(self):
        seen = []

        def handler(request):
            seen.append((request.path, request.params.get("model")))
            return 200, {
                "default_generation_settings": {"n_ctx": 32768}, "build_info": "b12560-6aee50801",
                "chat_template_caps": {"reasoning_effort_levels": ["low", "medium", "xhigh"],
                                       "reasoning_effort_rejected": ["minimal", "max"]},
                "modalities": {"vision": True, "audio": False},
            }

        result = serve(handler, lambda client: client.properties("qwen"))
        self.assertEqual(result, ServerProperties(32768, "b12560-6aee50801", ["low", "medium", "xhigh"],
                                                  ["minimal", "max"], vision=True))
        self.assertEqual(seen, [("/props", "qwen")])

    def test_a_router_without_the_model_reports_no_context_size(self):
        def handler(request):
            return 200, {"role": "router", "default_generation_settings": {"n_ctx": 0}}

        self.assertEqual(serve(handler, lambda client: client.properties("qwen")), ServerProperties())

    def test_completion_returns_the_context_tokens(self):
        def handler(request):
            return 200, {**_REPLY, "usage": {"prompt_tokens": 40, "completion_tokens": 2, "total_tokens": 42}}

        message, tokens = serve(handler, lambda client: client.complete("qwen", [], [], 16))
        self.assertEqual((message["content"], tokens), ("Hi.", 42))

    def test_a_request_that_does_not_fit_raises_a_context_error(self):
        def handler(request):
            return 400, {"error": {
                "code": 400, "type": "exceed_context_size_error", "n_prompt_tokens": 33010, "n_ctx": 32768,
                "message": "request (33010 tokens) exceeds the available context size (32768 tokens)",
            }}

        with self.assertRaises(ContextLimitError) as raised:
            serve(handler, lambda client: client.complete("qwen", [], [], 16))
        self.assertEqual((raised.exception.tokens, raised.exception.n_ctx), (33010, 32768))
        self.assertEqual(str(raised.exception),
                         "The request needs 33,010 tokens, but llama-server holds 32,768 tokens of context per slot.")

    def test_other_error_replies_name_the_endpoint_and_the_message(self):
        def handler(request):
            return 503, {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}}

        with self.assertRaisesRegex(ServerError, "^/health returned HTTP 503: Loading model$"):
            serve(handler, lambda client: client.check_health())

    def test_thinking_settings_are_sent_only_with_thinking(self):
        payloads = []

        def handler(request):
            payloads.append(request.json)
            return 200, _REPLY

        async def use(client):
            await client.complete("qwen", [], [], 2048, thinking_budget=1024, reasoning_effort="low")
            await client.complete("qwen", [], [], 2048, thinking=False, thinking_budget=1024, reasoning_effort="low")
            await client.complete("qwen", [], [], 2048)

        serve(handler, use)
        self.assertEqual(payloads[0]["reasoning_budget_tokens"], 1024)
        self.assertEqual(payloads[0]["reasoning_budget_message"], f"\n\n{THINKING_BUDGET_MESSAGE}\n")
        self.assertEqual(payloads[0]["reasoning_effort"], "low")
        fields = {"reasoning_budget_tokens", "reasoning_budget_message", "reasoning_effort"}
        self.assertFalse(fields & (set(payloads[1]) | set(payloads[2])))


class TransportTest(unittest.TestCase):
    def test_requests_keep_the_url_path_query_headers_and_text(self):
        seen = []

        def handler(request):
            seen.append((request.path, request.params, request.headers.get("Authorization"), request.json))
            return 200, {"ok": True}

        async def use(client):
            await client.request("GET", "/props", params={"model": "qwen 3"})
            return await client.request("POST", "/v1/chat/completions", json={"content": "Grüße"})

        with LocalServer(handler) as server:
            client = HttpClient(server.url + "/llama/", headers={"Authorization": "Bearer key"})
            response = asyncio.run(use(client))
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(seen, [
            ("/llama/props", {"model": "qwen 3"}, "Bearer key", None),
            ("/llama/v1/chat/completions", {}, "Bearer key", {"content": "Grüße"}),
        ])

    def test_a_server_that_is_not_running_is_reported(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{probe.getsockname()[1]}"
        with self.assertRaisesRegex(ServerError, f"^Could not reach llama-server at {url}: "):
            asyncio.run(LlamaClient(HttpClient(url)).check_health())

    def test_a_slow_reply_times_out(self):
        def handler(request):
            time.sleep(1.5)
            return 200, _REPLY

        with self.assertRaisesRegex(ServerError, "^llama-server did not finish its reply within 0.3 s. Raise --timeout"):
            serve(handler, lambda client: client.complete("qwen", [], [], 16), timeout=0.3)

    def test_a_cancelled_request_stops_waiting_for_the_reply(self):
        def handler(request):
            time.sleep(3)
            return 200, _REPLY

        async def use(client):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(client.complete("qwen", [], [], 16), 0.3)

        started = time.monotonic()
        serve(handler, use)
        # asyncio.run waits for the worker thread, so a quick return shows that the thread stopped reading.
        self.assertLess(time.monotonic() - started, 2)

    def test_localhost_reaches_a_server_that_listens_on_ipv4_only(self):
        with LocalServer(lambda request: (200, {"status": "ok"})) as server:
            client = HttpClient(f"http://localhost:{server.server.server_port}")
            started = time.monotonic()
            for _ in range(3):
                asyncio.run(client.request("GET", "/health"))
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(client.last_address[0], socket.AF_INET)

    def test_invalid_urls_are_rejected(self):
        for url in ("localhost:8080", "ftp://localhost", "http://localhost:port", "http://localhost:8080/?a=1"):
            with self.assertRaisesRegex(ValueError, "Invalid llama-server URL"):
                HttpClient(url)


if __name__ == "__main__":
    unittest.main()
