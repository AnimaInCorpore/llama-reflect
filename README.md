# Reflect

`refl` is a local coding agent for `llama-server` with persistent conversations and read-only verification and reflection forks.

Install from this directory using Python 3.9 or newer. Use `python3` if that is how your system names Python.

```text
python -m pip install -e .
refl --help
```

Start `llama-server` with a GGUF and chat template that support tool calling. The same `refl` arguments work in PowerShell, MSYS2, Linux, and macOS.

```text
llama-server -m model.gguf --jinja -np 3 --port 8081
```

Start Reflect from the root of the project you want to work on. Reflect uses that directory for its tools, sessions, and skills. Use `--workspace` to select a project explicitly when launching from elsewhere.

```text
cd path/to/project
refl --url http://127.0.0.1:8081
```

An empty directory works. Add an optional `REFLECT.md` at the project root for goals, conventions, and acceptance checks that apply across sessions.

Omit the prompt for an interactive session. Type `/exit` or send EOF to quit. Use `--model` to select a model, `LLAMA_BASE_URL` to set the default server, and `LLAMA_API_KEY` if the server requires authentication. You can also run `python -m refl` from this checkout.

## Interactive commands

- `/reflect` runs the three read-only reviewers on the current conversation and shows their proposals. Enter your next task when you are ready to continue.
- `/tree` shows the saved branches, their node IDs and statuses, and the current conversation node.
- `/checkout <node_id>` switches the conversation to that node. The next prompt grows a new branch and operates on the current workspace files.

You can inspect a saved tree with `refl --resume SESSION_ID "/tree"` while the server is stopped. For one-shot slash commands entered in MSYS2, prefix the invocation with `MSYS2_ARG_CONV_EXCL='/tree;/checkout;/reflect'` so the shell passes the command through as typed. Reflect reports a command that MSYS2 converted to a Windows path, and an unknown single-word command such as `/tre`, instead of sending it to the model.

Put `rg` (ripgrep) on `PATH` for `grep_search`. On Linux and macOS, `bash` uses Bash from `PATH`. On Windows, Reflect looks for Bash from MSYS2 or Git for Windows. Pass `--bash` with the executable path to choose a specific installation.

The file tools work with UTF-8 text under the workspace. `edit_file` requires a unique literal match and adapts inserted line endings to the file. The `bash` tool starts in the workspace and executes commands with your user permissions.

## Sessions and verification

Reflect saves the main conversation and its forks in `.refl/sessions/<session-id>.json` inside the workspace and adds a `.gitignore` there so Git skips them. It prints the session ID at startup and saves when a prompt is accepted and after each completed round. Use `--resume <session-id>` in the same workspace to continue the saved main branch in a new session.

When a completed model reply contains `<verify>specific claim</verify>`, Reflect starts a verifier with the conversation up to that point. Each verifier has up to three model requests and can use `read_file` and `grep_search`. Distinct claims in the same reply run concurrently. Tool calls in that reply are deferred so the main branch can act on the verdicts. A claim checked earlier in the same task keeps its verdict until a Bash command or file change.

```text
refl "Before editing settings.py, emit <verify>settings.py defines MAX_RETRIES</verify>. Use the verdict to decide what to edit."
```

Reflect discovers reusable Markdown skills under `.refl/skills/`. When a tool or workflow succeeds, the main branch can record the command for future sessions.

Repeated failures and unchanged results can trigger three parallel reviewers. They can inspect the workspace and read project skills. The Existing patterns reviewer can also see the six most recent Git commit subjects when a repository and Git are available. Reflect gives the main branch the proposals and adapts how often it intervenes as the session continues. The terminal shows **Skills** and **Strategy** updates, and `.refl/strategies/<session-id>.md` records the proposals, attempts, and tool results.

The file tools protect existing shared agent instructions and configuration under `.claude/`, `.codex/`, `.pi/`, and `.agents/`, with an exception for skill directories. Reflect passes the same guidance to the model for Bash commands, which run with your user permissions.

- [Design and reflection tuning](docs/design.md)
- [Project skills and tool discovery](docs/project-skills.md)
- [Lessons from development retrospectives](docs/retrospective-lessons.md)
- [Benchmark plan for the first release](docs/benchmark-plan.md)

Run the offline tests from this checkout with `python -m unittest`. They use a scripted model and need no server.

Reflect is available under the [MIT License](LICENSE).
