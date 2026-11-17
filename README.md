# Reflect

`refl` is a local coding agent for `llama-server`. It keeps each conversation as a tree with persistent sessions, checks claims in read-only verification forks, and starts read-only reflection forks when it notices that a task has stalled.

## Install

Install from this directory using Python 3.9 or newer. Use `python3` if that is how your system names Python.

```text
python -m pip install -e .
refl --help
```

You can also run `python -m refl` from this checkout.

Put `rg` (ripgrep) on `PATH` for the `grep_search` tool. On Linux and macOS, the `bash` tool uses Bash from `PATH`. On Windows, Reflect looks for Bash from MSYS2 or Git for Windows. Pass `--bash` with the executable path to choose a specific installation.

## Start a server

Start `llama-server` with a GGUF and a chat template that support tool calling. Port 8080 is Reflect's default.

```text
llama-server -m model.gguf --jinja -c 32768
```

Reflect sends the whole active conversation with every request, so give the server a context size that fits your longest sessions. With `-np 3`, the three reviewers of a reflection batch run at the same time. With one slot they run one after another. Depending on the llama.cpp build, slots can share the context size, and `/slots` shows each slot's `n_ctx`.

A reasoning model can spend several thousand tokens thinking before it answers. If a reply stops with "Completion reached its token limit", raise `--max-tokens` or use `--thinking`.

Some chat templates, such as Gemma 4's, place an assistant's text after the results of its tool calls. Reflect checks the server's template once per session through `/apply-template` and adjusts the messages it sends when needed. [The coding loop](docs/design.md#the-coding-loop)

## Run

Start Reflect from the root of the project you want to work on. Reflect uses that directory for its tools, sessions, and skills. An empty directory works.

```text
cd path/to/project
refl
```

Omit the prompt for an interactive session, and type `/exit` or send EOF to quit. Pass a prompt to run one task and exit. Add an optional `REFLECT.md` at the project root for goals, conventions, and acceptance checks that apply across sessions.

| Option | Default | Purpose |
| --- | --- | --- |
| `--url URL` | `LLAMA_BASE_URL` or `http://localhost:8080` | `llama-server` base URL |
| `--model ID` | First ID from `/v1/models` | Model to request |
| `--workspace DIR` | Current directory | Project root for tools, sessions, and skills |
| `--resume SESSION_ID` | | Continue a saved conversation in a new session |
| `--bash PATH` | Detected | Bash executable for the `bash` tool |
| `--linear` | Off | Run the main loop without verification or reflection forks |
| `--no-verify` | Off | Keep strategic reflection and skip verification forks |
| `--thinking auto\|off\|main` | `auto` | Thinking step for every request, none, or the main branch only |
| `--max-turns N` | 20 | Model requests per prompt, counted across all branches |
| `--max-tokens N` | 2048 | Output tokens per request |
| `--timeout SECONDS` | 180 | Time per model request, including any wait for a free slot |

Set `LLAMA_API_KEY` if the server requires authentication.

`--thinking` sends `chat_template_kwargs` with `enable_thinking`, which Qwen3 and Gemma 4 templates read and other templates ignore. Without thinking, a reasoning model answers much faster and uses fewer tokens, but it may plan less carefully. `--thinking main` keeps thinking for the main branch, which decides what to do, and skips it for the forks.

## Interactive commands

- `/reflect` runs three read-only reviewers on the current conversation and shows their ideas. It uses the idea-generating roles, such as Known alternative, Analogy, and Naive shortcut. Enter your next task when you are ready to continue.
- `/tree` shows the saved branches, their node IDs and statuses, and the current conversation node.
- `/checkout <node_id>` switches the conversation to that node. The next prompt grows a new branch and operates on the current workspace files.
- `/exit` ends the session.

You can inspect a saved tree with `refl --resume SESSION_ID "/tree"` while the server is stopped. For one-shot slash commands entered in MSYS2, prefix the invocation with `MSYS2_ARG_CONV_EXCL='/tree;/checkout;/reflect'` so the shell passes the command through as typed. Reflect reports a command that MSYS2 converted to a Windows path, and an unknown single-word command such as `/tre`, instead of sending it to the model.

## Tools

The model works with five tools. `read_file` returns up to 80 numbered lines and says where to continue. `grep_search` returns up to 30 matching lines. `write_file` and `edit_file` change UTF-8 text files under the workspace, and `edit_file` requires a unique match. It also accepts a match that differs only in blank lines and trailing spaces, and it adapts inserted line endings to the file. `bash` starts in the workspace and runs commands with your user permissions. The model receives each result as plain text, with an `Error:` or `Exit code:` line when one applies. The terminal lists each call with its command, path, or search pattern.

The file tools protect existing instruction and configuration files of other coding agents: `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`, `CLAUDE.local.md`, and `.mcp.json` anywhere in the workspace, and files under `.agents/`, `.claude/`, `.codex/`, and `.pi/` outside their skill directories. Reflect passes the same rule to the model for Bash commands.

## Sessions

Reflect saves the main conversation and its forks in `.refl/sessions/<session-id>.json` inside the workspace and adds a `.gitignore` there so Git skips them. It prints the session ID at startup and saves when a prompt is accepted and after each completed round. Use `--resume <session-id>` in the same workspace to continue the saved main branch in a new session.

## Verification

When a completed model reply contains `<verify>specific claim</verify>`, Reflect starts a verifier with the conversation up to that point. Each verifier has up to three model requests and can use `read_file` and `grep_search`. Distinct claims in the same reply run concurrently. Tool calls in that reply are deferred so the main branch can act on the verdicts. A claim checked earlier in the same task keeps its verdict until a Bash command or file change.

```text
refl "Before editing settings.py, emit <verify>settings.py defines MAX_RETRIES</verify>. Use the verdict to decide what to edit."
```

## Strategic reflection

Repeated failures, unchanged results, edits that go back and forth, a final answer while a rerun check still fails, and a long stretch without any file change while a command still fails can start a reflection batch. Three read-only reviewers with different roles look at the stall. The first batch in a task looks for causes and evidence, and reviewers with workspace access must start with a check and assume nothing that no tool result shows. A later batch checks the prerequisites, such as the tools, versions, loaded modules, and configuration. Further stalls in the same task raise a reflection temperature that brings in alternative and lateral roles, such as switching tools, rebuilding parts of the code to bisect a fault, borrowing a mechanism from another field, or questioning a constraint. Each reviewer returns an idea with its basis, the cost of trying it, and the result that would show it failed.

Reflect gives the main branch the ideas in two groups, evidence-based and unverified. The main branch then chooses one idea and states its first step before it acts. The terminal shows **Skills** and **Strategy** updates, and `.refl/strategies/<session-id>.md` records the proposals, the plan, and the attempts that follow. [Strategic reflection](docs/design.md#strategic-reflection)

## Skills

Reflect discovers reusable Markdown skills under `.refl/skills/` and lists them for the model. When a tool or workflow succeeds, the main branch can record the command as a skill for future sessions. [Project skills](docs/project-skills.md)

## Known limitations

- Reflect sends the whole active conversation with every request and does not trim it yet, so long sessions need a large context window.
- The file tools read and write UTF-8 text only. Other encodings need Bash.
- The `bash` tool runs commands with your user permissions, without a sandbox or confirmation. Use a disposable folder or a container for tasks you do not trust.
- In tests with 27B models, reflection never started on tasks the model solved directly. It helped on a task where the model went in circles, until a newer llama.cpp build let the model solve that task directly. A 4B model stalls more often. With thinking, reflection solved a task that the model never solved alone. Without thinking, it gained nothing, because the model rarely acted on what the reviewers found. [Results so far](docs/benchmark-plan.md#results-so-far)

## Development

Run the offline tests from this checkout with `python -m unittest`. They use a scripted model and need no server.

`python bench/reflection_ab.py` runs paired tasks with and without reflection against a live server. `python bench/polyglot.py --polyglot PATH` runs the Python exercises of a local clone of Aider's Polyglot benchmark the same way. `prototype.py` is an early probe that checks whether the server runs two tool-calling branches at the same time.

- [Design](docs/design.md)
- [Project skills and tool discovery](docs/project-skills.md)
- [Lessons from development retrospectives](docs/retrospective-lessons.md)
- [Benchmark plan and results](docs/benchmark-plan.md)

Reflect is available under the [MIT License](LICENSE).
