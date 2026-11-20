import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import httpx

from .client import LlamaClient, ServerError
from .engine import THINKING_MODES, Engine, SYSTEM_PROMPT, TurnLimitError
from .tools import WorkspaceTools
from .tree import ConversationTree


_TREE_COMMANDS = {"/tree", "/checkout"}
_COMMANDS = _TREE_COMMANDS | {"/reflect", "/exit"}
_SLASH_WORD = re.compile(r"/[A-Za-z][\w-]*")
# MSYS2 rewrites a leading-slash argument such as /tree into C:/Program Files/Git/tree.
_MSYS_COMMAND = re.compile(r"[A-Za-z]:[\\/](?:[^\n]*[\\/])?(tree|checkout|reflect|exit)(?:\s+[^\n]*)?")


def _positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def _thinking_budget(value):
    number = int(value)
    if number < -1:
        raise argparse.ArgumentTypeError("expected -1 or a token count of 0 or more")
    return number


def _status(message):
    print(message, file=sys.stderr, flush=True)


def _tolerant_output(stream):
    """Escape characters the console encoding cannot represent instead of failing."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")


def _check_command(command, argument):
    if not argument and command not in _COMMANDS and _SLASH_WORD.fullmatch(command):
        raise ValueError(f"Unknown command {command}. Use /reflect, /tree, /checkout <node_id>, or /exit.")


def _open_tree(root, resume, *, persist=True):
    tree = ConversationTree(root / ".refl" / "sessions", SYSTEM_PROMPT, resume=resume, persist=persist)
    if persist:
        _status(f"Session: {tree.path.stem}")
    return tree


def _split_prompt(prompt):
    parts = prompt.split(maxsplit=1)
    return parts[0], parts[1] if len(parts) == 2 else ""


def _tree_command(tree, command, argument):
    if command == "/tree":
        if argument:
            raise ValueError("Usage: /tree")
        print(tree.render(), flush=True)
        return

    try:
        node_id = int(argument)
    except ValueError as error:
        raise ValueError("Usage: /checkout <node_id>") from error
    tree.checkout(node_id)
    _status(f"Conversation switched to node {node_id}. The next task uses the current workspace files.")


async def _submit(engine, prompt):
    command, argument = _split_prompt(prompt)
    _check_command(command, argument)
    if command in _TREE_COMMANDS:
        _tree_command(engine.tree, command, argument)
        if command == "/checkout":
            engine.track_strategy = False
    elif command == "/reflect":
        if argument:
            raise ValueError("Usage: /reflect")
        await engine.reflect()
    elif command == "/exit":
        raise ValueError("Usage: /exit")
    else:
        print(await engine.respond(prompt), flush=True)


async def _run(args):
    root = args.workspace.resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace directory does not exist: {root}")
    if args.prompt is not None:
        command, argument = _split_prompt(args.prompt)
        _check_command(command, argument)
        if command == "/exit":
            if argument:
                raise ValueError("Usage: /exit")
            return
        if command in _TREE_COMMANDS:
            tree = _open_tree(root, args.resume, persist=False)
            _tree_command(tree, command, argument)
            if command == "/checkout":
                _status(f"Session: {tree.path.stem}")
            return
    workspace = WorkspaceTools(root, bash=args.bash)
    if args.bash and workspace.bash_executable is None:
        raise ValueError(f"Bash executable not found: {args.bash}")

    key = os.environ.get("LLAMA_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    timeout = httpx.Timeout(args.timeout, connect=5)
    async with httpx.AsyncClient(base_url=args.url, headers=headers, timeout=timeout) as http:
        server = LlamaClient(http)
        await server.check_health()
        model = args.model or await server.default_model()
        _status(f"Model: {model}\nWorkspace: {root}")
        try:
            slots, busy = await server.slots()
            _status(f"Slots: {busy}/{slots} busy")
        except ServerError as error:
            _status(f"Slot status unavailable ({error})")
        try:
            context_size, build = await server.properties(model)
        except ServerError as error:
            context_size, build = None, None
            _status(f"Server properties unavailable ({error})")
        else:
            _status(f"Context: {context_size:,} tokens per slot" if context_size else "Context size unavailable")
        if build:
            _status(f"Server build: {build}")

        tree = _open_tree(root, args.resume)
        engine = Engine(server, model, workspace, tree, args.max_turns, args.max_tokens, _status,
                        forks=not args.linear, verification=not args.no_verify, thinking=args.thinking,
                        thinking_budget=args.thinking_budget, context_size=context_size)
        if args.prompt is not None:
            await _submit(engine, args.prompt)
            return

        interactive = sys.stdin.isatty()
        if interactive:
            _status("Enter a task or use /reflect, /tree, /checkout <node_id>, or /exit.")
        while True:
            try:
                prompt = input("refl> " if interactive else "").strip()
            except EOFError:
                return
            if prompt == "/exit":
                return
            if prompt:
                try:
                    await _submit(engine, prompt)
                except (ServerError, TurnLimitError, ValueError) as error:
                    if not interactive:
                        raise
                    _status(f"refl: {error}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="refl", description="Local coding agent for llama-server")
    parser.add_argument("prompt", nargs="?", help="one task or slash command (omit for interactive mode)")
    parser.add_argument("--url", default=os.environ.get("LLAMA_BASE_URL", "http://localhost:8080"),
                        help="llama-server base URL (default: http://localhost:8080)")
    parser.add_argument("--model", help="model ID (default: first ID from /v1/models)")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="workspace root (default: current directory)")
    parser.add_argument("--resume", metavar="SESSION_ID", help="continue a saved conversation in a new session")
    parser.add_argument("--bash", help="Bash executable (default: detect Bash)")
    parser.add_argument("--linear", action="store_true", help="run the main tool loop with both kinds of forks disabled")
    parser.add_argument("--no-verify", action="store_true",
                        help="disable verification forks and keep strategic reflection")
    parser.add_argument("--thinking", choices=THINKING_MODES, default="auto",
                        help="auto: the chat template decides; off: ask it to skip thinking for every request; "
                             "main: think on the main branch only (default: auto)")
    parser.add_argument("--max-turns", type=_positive_int, default=20,
                        help="model requests per prompt across all branches (default: 20)")
    parser.add_argument("--max-tokens", type=_positive_int, default=2048, help="output tokens per completion (default: 2048)")
    parser.add_argument("--thinking-budget", type=_thinking_budget, metavar="N",
                        help="thinking tokens per request before the server ends the thinking block; -1 sets no "
                             "limit (default: --max-tokens minus room for the answer)")
    parser.add_argument("--timeout", type=_positive_int, default=180, help="model request timeout in seconds (default: 180)")
    args = parser.parse_args(argv)
    if args.prompt is not None and not args.prompt.strip():
        parser.error("prompt must be nonempty")
    if args.prompt is not None and (mangled := _MSYS_COMMAND.fullmatch(args.prompt.strip())):
        parser.error(
            f"the prompt looks like /{mangled[1]} rewritten as a Windows path by MSYS2. Set "
            "MSYS2_ARG_CONV_EXCL='/tree;/checkout;/reflect' for one-shot slash commands, as described in the README."
        )
    for stream in (sys.stdout, sys.stderr):
        _tolerant_output(stream)
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except (httpx.InvalidURL, OSError, RuntimeError, ValueError) as error:
        _status(f"refl: {error}")
        return 1
    return 0
