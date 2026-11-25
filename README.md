# Reflect

`refl` is a local coding agent for `llama-server`. It keeps each conversation as a tree with persistent sessions, checks claims in read-only verification forks, has critics challenge the plan before the first file change of a task, and starts read-only reflection forks when it notices that a task has stalled.

## Install

Reflect needs Python 3.9 or newer and nothing beyond its standard library. Install it from this directory. Use `python3` if that is how your system names Python.

```text
python -m pip install -e .
refl --help
```

To build the package, pip downloads `setuptools`. Without internet access, add `--no-build-isolation`, which uses the `setuptools` of your Python installation if it has one. You can also skip the install and run `python -m refl` from this checkout.

The `grep_search` tool uses `rg` (ripgrep) when it is on `PATH`. Without it, Reflect uses a built-in Python search that finds the same lines but is slower in large projects. On Linux and macOS, the `bash` tool uses Bash from `PATH`. Pass `--bash` with the executable path to choose a specific installation.

### Windows

In PowerShell, install Reflect from this directory and call it through Python:

```powershell
python -m pip install -e .
python -m refl --help
```

pip often puts `refl.exe` in a `Scripts` folder that is not on `PATH`, for example with Python from the Microsoft Store, and names that folder in a warning. After the install, `python -m refl` works from any directory, so use it wherever this README calls `refl`. Python looks for the modules it needs to start `-m` in the current folder first, so a project file such as `types.py` stops it with "Could not import runpy module". Start Reflect from another folder with `--workspace` in that case.

The `bash` tool needs Bash from MSYS2 or Git for Windows. Reflect finds `C:\msys64\usr\bin\bash.exe` and the Bash that comes with Git for Windows on its own. It skips the `bash.exe` in the Windows folder, which starts WSL.

ripgrep is optional. winget installs it and adds it to `PATH` for new PowerShell windows:

```powershell
winget install --id BurntSushi.ripgrep.MSVC --exact
```

The ripgrep of MSYS2 in `C:\msys64\ucrt64\bin` works too. Add that folder to the end of `PATH`, because it also contains a `python.exe` that would otherwise take the place of your usual Python.

Start `llama-server` in its own PowerShell window with the call operator `&`, and quote paths that contain spaces. If another program already uses port 8080, choose another port:

```powershell
& 'C:\path\to\llama-server.exe' -m 'C:\path\to\model.gguf' --jinja -c 32768 --port 8091
```

Then start Reflect in the project folder with the same address:

```powershell
cd C:\path\to\project
python -m refl --url http://127.0.0.1:8091
```

PowerShell sets environment variables such as `LLAMA_API_KEY` and `LLAMA_BASE_URL` with `$env:`, for example `$env:LLAMA_BASE_URL = "http://127.0.0.1:8091"`. The setting lasts until the window closes.

### MSYS2

Reflect also runs in the MSYS2 UCRT64 shell with MSYS2's own Python, straight from the checkout and without an install. Open "MSYS2 UCRT64" from the Start menu. Reflect has not been tested with the Python of the plain MSYS shell. Install Python in UCRT64 if it is missing, and ripgrep if you want faster searches:

```bash
pacman -S mingw-w64-ucrt-x86_64-python mingw-w64-ucrt-x86_64-ripgrep
```

This Python has no pip and is managed by pacman, which does not matter, because Reflect needs no packages. Add a `refl` function to `~/.bashrc` that runs Reflect from the checkout, then load it:

```bash
echo 'refl() { PYTHONPATH=/c/path/to/llama-reflect python -m refl "$@"; }' >> ~/.bashrc
source ~/.bashrc
```

If `llama-server` uses a port other than 8080, add its address to `~/.bashrc` as well, for example `export LLAMA_BASE_URL=http://127.0.0.1:8091`. MSYS2 converts paths such as `/c/models/model.gguf` for Windows programs, so you can start `llama-server.exe` from the same shell. Then start Reflect in the project folder:

```bash
cd /c/path/to/project
refl
```

Because Reflect runs from the checkout, a `git pull` there takes effect the next time you start it. Windows programs do not always see the MSYS2 window as a terminal. If Reflect shows no `refl>` prompt there, install the `winpty` package and put `winpty` before `python` in the function. Single tasks passed as an argument work either way. Slash commands passed as an argument need the prefix described under [Interactive commands](#interactive-commands).

## Start a server

Start `llama-server` with a GGUF and a chat template that support tool calling. Port 8080 is Reflect's default.

```text
llama-server -m model.gguf --jinja -c 32768
```

Reflect sends the whole active conversation with every request, so give the server a context size that fits your longest sessions. At startup Reflect shows the context size per slot and the server build from `/props`. It warns when the main conversation fills 75% and 90% of the context, and it explains how to continue when a request no longer fits. With `-np 3`, the three reviewers of a reflection batch run at the same time. With one slot they run one after another. Depending on the llama.cpp build, slots can share the context size.

With `--thinking auto` or `main`, a reasoning model can spend several thousand tokens thinking before it answers. Reflect therefore gives each request a thinking budget. When the thinking reaches it, `llama-server` ends the thinking block, and the model answers within the rest of `--max-tokens`. The terminal then shows "Thinking stopped at its budget". If the answer itself still does not fit, which can happen while the model writes a large file, Reflect discards the cut-off reply and asks once for a shorter one. When that one is cut off too, the turn stops with "Completion reached its token limit", so raise `--max-tokens`. The limit reserves no memory in `llama-server`, but a long reply takes time: at about 45 tokens per second, 16,384 tokens take about six minutes, within the default `--timeout` of 600 seconds. On a slower machine, lower `--max-tokens` or raise `--timeout`. Builds of `llama-server` from before July 2026 ignore the budget.

Some chat templates, such as Gemma 4's, place an assistant's text after the results of its tool calls. Reflect checks the server's template once per session through `/apply-template` and adjusts the messages it sends when needed. [The coding loop](docs/design.md#the-coding-loop)

Reflect also works with Basalt, which serves the same endpoints except `/apply-template`. Set `LLAMA_BASE_URL` to its address without `/v1` and `LLAMA_API_KEY` to its key. Each request carries an `x-session-id` header, one for the main conversation and one per verifier or reviewer, so Basalt can keep each conversation on one slot and reuse its cached context. `llama-server` ignores the header. Basalt counts `--max-tokens` against the context of a slot, so with the default of 16,384 a conversation fits in 16,384 tokens less than the slot holds. A lower `--max-tokens` leaves more room.

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
| `--linear` | Off | Run the main loop without verification, challenge, or reflection forks |
| `--no-verify` | Off | Keep strategic reflection and skip verification forks |
| `--no-challenge` | Off | Skip the critics that challenge the plan before the first file change of a task |
| `--thinking off\|auto\|main` | `off` | No thinking step, one when the chat template decides so, or one on the main branch only |
| `--fork-effort LEVEL` | Template default | Reasoning effort for verifier and reviewer requests, such as `low` |
| `--max-turns N` | 20 | Model requests per prompt, counted across all branches. When they run out, one more request without tools reports where the task stands, and a follow-up such as `continue` goes on from there |
| `--max-tokens N` | 16384 | Output tokens per request |
| `--thinking-budget N` | Half of `--max-tokens` | Thinking tokens per request before the server ends the thinking block. `-1` sets no limit |
| `--timeout SECONDS` | 600 | Time per model request, including any wait for a free slot |
| `--color WHEN` | `auto` | `auto`, `always`, or `never`. `auto` colors the output in a terminal unless `NO_COLOR` is set |

Set `LLAMA_API_KEY` if the server requires authentication.

In a terminal, each kind of output has its own style. Main-branch tool calls show the tool name in bold, and the final answer is bold. Verification is cyan, with `VERIFIED` in green, `FALSE ASSUMPTION` in red, and `UNVERIFIED` in yellow. The plan challenge, its critics, and the main branch's answers to them are magenta, and strategic reflection is blue. Skills and status lines are dim, warnings are yellow, and errors are red. Piped or redirected output stays plain. On Windows, Reflect turns on ANSI colors in the console itself, and it also recognizes the mintty window of MSYS2 and Git Bash, which native programs do not see as a terminal.

Thinking is off by default. `--thinking` sends `chat_template_kwargs` with `enable_thinking`, which Qwen3 and Gemma 4 templates read and other templates ignore, so a model with another template may think anyway. Without thinking, a reasoning model answers much faster and uses fewer tokens, but it may plan less carefully. At about 45 tokens per second, a request that thinks through the default budget of 8,192 tokens waits about three minutes before it acts. In the benchmark's natural runs, two 27B models solved every run with and without thinking. [Results so far](docs/benchmark-plan.md#results-so-far) `--thinking auto` lets the chat template decide, and Qwen3.8's template then thinks. `--thinking main` keeps thinking for the main branch, which decides what to do, and skips it for the forks. `--fork-effort` keeps thinking in the forks at a lower or higher effort, for chat templates with effort levels, such as Qwen3.8's, and needs `--thinking auto`. At startup Reflect lists the template's levels and stops on a level the template rejects. The default budget is half of `--max-tokens`, so with thinking, 8,192 of the default 16,384 tokens go to thinking and the rest stays for the answer.

## Interactive commands

- `/reflect` runs three read-only reviewers on the current conversation and shows their ideas. It uses the idea-generating roles, such as Known alternative, Analogy, and Naive shortcut. Enter your next task when you are ready to continue.
- `/tree` shows the saved branches, their node IDs and statuses, and the current conversation node.
- `/checkout <node_id>` switches the conversation to that node. The next prompt grows a new branch and operates on the current workspace files.
- `/exit` ends the session.

You can inspect a saved tree with `refl --resume SESSION_ID "/tree"` while the server is stopped. For one-shot slash commands entered in MSYS2, prefix the invocation with `MSYS2_ARG_CONV_EXCL='/tree;/checkout;/reflect'` so the shell passes the command through as typed. Reflect reports a command that MSYS2 converted to a Windows path, and an unknown single-word command such as `/tre`, instead of sending it to the model.

## Tools

The model works with five tools, and a sixth when the server takes images. `read_file` returns up to 80 numbered lines and says where to continue. `grep_search` returns up to 30 matching lines. `write_file` and `edit_file` change UTF-8 text files under the workspace, and `edit_file` requires a unique match. It also accepts a match that differs only in blank lines and trailing spaces, and it adapts inserted line endings to the file. `bash` starts in the workspace and runs commands with your user permissions. The model receives each result as plain text, with an `Error:` or `Exit code:` line when one applies. When `llama-server` runs a vision model with its `--mmproj` file, Reflect shows "Image input" at startup and adds `view_image`, which shows the model a PNG, JPEG, GIF, or BMP file of up to 32 MiB and 64 megapixels from the workspace. The model can then check a screenshot, a plot, or its own drawing. It converts other formats, such as SVG, to PNG with Bash first, for example with `rsvg-convert`, which MSYS2 provides. The terminal lists each call with its command, path, or search pattern.

The file tools protect existing instruction and configuration files of other coding agents: `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`, `CLAUDE.local.md`, and `.mcp.json` anywhere in the workspace, and files under `.agents/`, `.claude/`, `.codex/`, and `.pi/` outside their skill directories. Reflect passes the same rule to the model for Bash commands. Every role also gets a dependency rule: solve the task with the tools and libraries that are already available, install nothing, and ask the user before trying a missing tool or library.

## Sessions

Reflect saves the main conversation and its forks in `.refl/sessions/<session-id>.json` inside the workspace and adds a `.gitignore` there so Git skips them. It prints the session ID at startup and saves when a prompt is accepted and after each completed round. Use `--resume <session-id>` in the same workspace to continue the saved main branch in a new session.

## Verification

When a completed model reply contains `<verify>specific claim</verify>`, Reflect starts a verifier with the conversation up to that point. Each verifier has up to three model requests and can use `read_file`, `grep_search`, and `view_image` when the server takes images. Distinct claims in the same reply run concurrently. Tool calls in that reply are deferred so the main branch can act on the verdicts. A claim checked earlier in the same task keeps its verdict until a Bash command or file change.

```text
refl "Before editing settings.py, emit <verify>settings.py defines MAX_RETRIES</verify>. Use the verdict to decide what to edit."
```

## Plan challenge

Before the first `write_file` or `edit_file` call of a task, Reflect holds the call back and starts four read-only critics. Three assume that the plan will fail: Requirements compares it with the request and any spec, tests, or documentation, Environment checks what it assumes about files, tools, and configuration, and Edge cases looks for an input or caller that breaks. Each reads the workspace once and names its most likely objection with evidence and a check. The fourth, Simpler plan, steps back to the goal at the highest level of abstraction, what must be true when the task is done, and derives the simplest strategy that reaches it with what the workspace and the system already provide. A verifier then checks each finding in the workspace. For a simpler strategy, it checks what the strategy relies on and that it keeps every requirement. Refuted objections and unproven strategies are dropped, and the main branch gets the rest. It has to answer each objection, and say whether it switches to a proven simpler strategy, without tools. Each critic whose finding was proven then judges that answer once, and a point it still finds open goes back to the main branch one more time. Then the main branch reissues its change or a better one.

The challenge runs once per task, skips changes under `.refl/`, and does not catch changes made only through Bash. It also skips small edits: an `edit_file` call that replaces at most 3 lines and 200 characters with as few, or a `write_file` call of that size to a new or equally small file. The task's first larger change still gets the challenge. Its requests come from a budget of 30 of their own, so they do not count toward `--max-turns`. With three server slots, three critics run at the same time and the fourth waits for a slot. On a 27B model a challenge took about half a minute. In that test the model solved the task without the challenge as well, because a check in the workspace caught its first attempt. [Plan challenge](docs/design.md#plan-challenge)

## Strategic reflection

Repeated failures, unchanged results, edits that go back and forth, a final answer while a rerun check still fails, and a long stretch without any file change while a command still fails can start a reflection batch. Three read-only reviewers with different roles look at the stall. The first batch in a task looks for causes and evidence, and reviewers with workspace access must start with a check and assume nothing that no tool result shows. A later batch checks the prerequisites, such as the tools, versions, loaded modules, and configuration. Further stalls in the same task raise a reflection temperature that brings in alternative and lateral roles, such as switching tools, rebuilding parts of the code to bisect a fault, borrowing a mechanism from another field, or questioning a constraint. Each reviewer returns an idea with its basis, the cost of trying it, and the result that would show it failed.

Reflect gives the main branch the ideas in two groups, evidence-based and unverified. The main branch then chooses one idea and states its first step before it acts. The terminal shows **Skills** and **Strategy** updates, and `.refl/strategies/<session-id>.md` records the proposals, the plan, and the attempts that follow. [Strategic reflection](docs/design.md#strategic-reflection)

## Skills

Reflect discovers reusable Markdown skills under `.refl/skills/` and lists them for the model. When a tool or workflow succeeds, the main branch can record the command as a skill for future sessions. [Project skills](docs/project-skills.md)

## Known limitations

- Reflect sends the whole active conversation with every request and does not trim it yet, so long sessions need a large context window. When a conversation outgrows it, restart `llama-server` with more context per slot and continue with `--resume`.
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
