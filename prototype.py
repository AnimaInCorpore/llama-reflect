"""Probe two concurrent llama-server branches. Requires Python 3.9+, httpx, and ripgrep (rg)."""

import argparse
import asyncio
import os
from pathlib import Path
from time import perf_counter

import httpx

from refl.tools import TOOLS as WORKSPACE_TOOLS, WorkspaceTools

TOOLS = [tool for tool in WORKSPACE_TOOLS if tool["function"]["name"] in {"read_file", "grep_search"}]


async def complete(client, model, messages):
    response = await client.post("/v1/chat/completions", json={
        "model": model, "messages": messages, "tools": TOOLS,
        "temperature": 0, "max_tokens": 256,
    })
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"], data["usage"]["completion_tokens"]


async def probe(client, model, workspace, name, prompt):
    started = perf_counter()
    messages = [
        {"role": "system", "content": "Use the requested tool once, then answer briefly from its result."},
        {"role": "user", "content": prompt},
    ]
    message, tokens = await complete(client, model, messages)
    calls = message.get("tool_calls") or []
    if len(calls) != 1 or calls[0]["function"]["name"] != name:
        raise RuntimeError(f"Model did not call {name} exactly once. Check the tool-use chat template.")
    call = calls[0]
    observation = await asyncio.to_thread(workspace.execute, call["function"])
    if "error" in observation:
        raise RuntimeError(f"{name} failed: {observation['error']}")
    messages.extend([
        message,
        {"role": "tool", "tool_call_id": call["id"], "content": observation["output"]},
    ])
    answer, followup_tokens = await complete(client, model, messages)
    if answer.get("tool_calls") or not answer.get("content"):
        raise RuntimeError(f"Model did not answer after {name} returned its result.")
    finished = perf_counter()
    print(f"{name} ({finished - started:.2f}s): {answer['content'].strip()}")
    return finished, tokens + followup_tokens


async def get_slots(client):
    response = await client.get("/slots", timeout=2)
    response.raise_for_status()
    slots = response.json()
    return len(slots), sum(slot["is_processing"] for slot in slots)


async def run(args):
    key = os.environ.get("LLAMA_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(base_url=args.url, headers=headers, timeout=120) as client:
        (await client.get("/health", timeout=5)).raise_for_status()
        model = args.model
        if not model:
            response = await client.get("/v1/models", timeout=5)
            response.raise_for_status()
            model = response.json()["data"][0]["id"]
        print(f"Model: {model}")
        try:
            slots, busy = await get_slots(client)
            print(f"Slots at startup: {busy}/{slots} busy")
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            print(f"Slot monitoring unavailable: {error}")
            slots = 0

        workspace = WorkspaceTools(Path.cwd())
        started = perf_counter()
        batch = asyncio.gather(
            probe(client, model, workspace, "read_file", "Read lines 1 through 3 of prototype.py and describe its purpose."),
            probe(client, model, workspace, "grep_search", "Search prototype.py for the regex 'asyncio[.]gather'. Report its line number."),
            return_exceptions=True,
        )
        peak = None
        while slots and not batch.done():
            try:
                _, busy = await get_slots(client)
                if not batch.done():
                    peak = busy if peak is None else max(peak, busy)
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                print(f"Slot monitoring stopped: {error}")
                break
            await asyncio.wait({batch}, timeout=0.05)

        results = await batch
        if peak is not None:
            print(f"Peak server-wide busy slots during probe: {peak}/{slots}")
        elif slots:
            print("No slot samples collected during the probe.")
        for result in results:
            if isinstance(result, Exception):
                raise result
        elapsed = max(finished for finished, _ in results) - started
        tokens = sum(count for _, count in results)
        print(f"Both branches completed in {elapsed:.2f}s ({tokens / elapsed:.1f} output tokens/s end to end).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8080", help="llama-server base URL")
    parser.add_argument("--model", help="Model ID (default: first model listed by the server)")
    try:
        asyncio.run(run(parser.parse_args()))
    except (httpx.HTTPError, IndexError, KeyError, RuntimeError, TypeError, ValueError) as error:
        parser.exit(1, f"Probe failed: {error}\n")
