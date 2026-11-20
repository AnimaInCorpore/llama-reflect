import argparse
import asyncio
import os
import re
import sys
from pathlib import Path


from .client import LlamaClient, ServerError, ServerProperties
from .transport import HttpClient
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


def _check_fork_effort(effort, properties):
    """Stop on a level the chat template rejects, and warn about one that is not among its levels."""
    if effort is None or properties.effort_levels is None:
        return
    levels = ", ".join(properties.effort_levels)
    if effort in (properties.rejected_efforts or []):
        raise ValueError(f"The chat template rejects --fork-effort {effort}. Its levels are {levels}.")
    if not properties.effort_levels:
        _status(f"The chat template has no reasoning effort levels, so --fork-effort {effort} has no effect.")
    elif effort not in properties.effort_levels:
        _status(f"--fork-effort {effort} is not one of the chat template's levels ({levels}), so the template "
                f"treats it like one of them or like its default.")


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
    server = LlamaClient(HttpClient(args.url, headers=headers, timeout=args.timeout))
    await server.check_health()
    model = args.model or await server.default_model()
    _status(f"Model: {model}\nWorkspace: {root}")
    try:
        slots, busy = await server.slots()
        _status(f"Slots: {busy}/{slots} busy")
    except ServerError as error:
        _status(f"Slot status unavailable ({error})")
    try:
        properties = await server.properties(model)
    except ServerError as error:
        properties = ServerProperties()
        _status(f"Server properties unavailable ({error})")
    else:
        _status(f"Context: {properties.context_size:,} tokens per slot" if properties.context_size
                else "Context size unavailable")
    if properties.build:
        _status(f"Server build: {properties.build}")
    if properties.effort_levels:
        _status(f"Reasoning effort levels: {', '.join(properties.effort_levels)}")
    if properties.vision:
        _status("Image input: the model can look at images with view_image")
    _check_fork_effort(args.fork_effort, properties)

    tree = _open_tree(root, args.resume)
    engine = Engine(server, model, workspace, tree, args.max_turns, args.max_tokens, _status,
                    forks=not args.linear, verification=not args.no_verify, thinking=args.thinking,
                    thinking_budget=args.thinking_budget, fork_effort=args.fork_effort,
                    context_size=properties.context_size, vision=properties.vision is True)
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
    parser.add_argument("--thinking", choices=THINKING_MODES, default="off",
                        help="off: ask the chat template to skip thinking for every request; auto: the chat template "
                             "decides; main: think on the main branch only (default: off)")
    parser.add_argument("--fork-effort", metavar="LEVEL",
                        help="reasoning effort for verifier and reviewer requests, such as low; the chat template "
                             "lists its levels at startup (default: the template's default)")
    parser.add_argument("--max-turns", type=_positive_int, default=20,
                        help="model requests per prompt across all branches (default: 20)")
    parser.add_argument("--max-tokens", type=_positive_int, default=16384, help="output tokens per completion (default: 16384)")
    parser.add_argument("--thinking-budget", type=_thinking_budget, metavar="N",
                        help="thinking tokens per request before the server ends the thinking block; -1 sets no "
                             "limit (default: half of --max-tokens)")
    parser.add_argument("--timeout", type=_positive_int, default=600, help="model request timeout in seconds (default: 600)")
    args = parser.parse_args(argv)
    if args.prompt is not None and not args.prompt.strip():
        parser.error("prompt must be nonempty")
    if args.fork_effort == "none":
        parser.error("use --thinking main to skip thinking in the forks")
    if args.fork_effort is not None and args.thinking != "auto":
        parser.error(f"--fork-effort needs --thinking auto, because the forks skip thinking with --thinking "
                     f"{args.thinking}{', the default' if args.thinking == 'off' else ''}")
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
    except (OSError, RuntimeError, ValueError) as error:
        _status(f"refl: {error}")
        return 1
    return 0
