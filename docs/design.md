# Reflect design

## The coding loop

`refl` sends the active conversation to the OpenAI-compatible chat endpoint in `llama-server`. The model can inspect UTF-8 files with `read_file` and `grep_search`, run Bash commands with `bash`, and make changes with `write_file` and `edit_file`. Reflect executes calls in the order they appear and returns each observation with the corresponding tool call ID. The model continues until it returns a final answer or reaches the request limit.

The CLI and file tools use Python filesystem APIs on all platforms. PowerShell and MSYS2 launch the same CLI. The `bash` tool invokes Bash from the workspace directory. On Windows, Bash can come from MSYS2 or Git for Windows. `rg` is an external executable used by `grep_search` on all four target platforms.

The launch directory is the workspace unless `--workspace` selects one explicitly. An empty workspace can start a session. When `REFLECT.md` exists at the workspace root, the model reads it as project guidance. The file tools preserve existing instructions and configuration from other coding agents, and allow writes in their skill directories. Bash runs with the launching user's permissions and receives the project's preservation rule as model guidance.

## Conversation trees

Each session starts with a system message and a main branch. Each node records its ID, parent ID, branch type, status, and the chat message, including any tool calls or tool results. The `main_head` in the session file points to the active conversation node. Following parent IDs back to the root and reversing that path reconstructs the conversation for a given branch.

```mermaid
flowchart TD
    A[System message] --> B[User task]
    B --> C[Assistant emits a verification tag]
    C --> D[Verifier receives the claim]
    D --> E[Verifier reads workspace evidence]
    E --> F[Verifier returns a verdict]
    C --> G[Main branch receives the verdict]
    F -. verdict text .-> G
    G --> H[Main branch continues the task]
```

Solid arrows are parent links in the saved tree. The dotted arrow is the verdict copied into the main conversation. A verifier's tool calls and observations remain on its branch. Nodes from a fork with a usable verdict are marked `merged`. Forks that fail or exhaust the request budget are marked `pruned` and retained in the session for inspection.

Reflect writes UTF-8 JSON to `.refl/sessions/<session-id>.json` at startup, when it accepts a prompt, and after each completed model and tool round. It writes to a temporary file in the same directory and replaces the session file once the snapshot is complete. The session ID includes a UTC timestamp and a unique suffix. The project ignores session files in Git because they can hold prompts, file contents, and command output.

```text
refl --workspace ./project --resume SESSION_ID "Continue with the next step."
```

Resuming copies the saved tree into a new session file and continues from the saved active node. The copied tree includes earlier forks. A saved turn interrupted before a final answer is closed with a brief transcript note when the next prompt begins. The CLI prints the new session ID at startup.

`/tree` displays every branch and marks the current node. `/checkout <node_id>` can select a main, verification, or reflection node. The next prompt extends the selected path, and earlier branches remain visible in the tree. If the selected node falls inside a tool round, Reflect adds skipped results for tool calls still awaiting a result. The next task operates on the workspace as it exists when the task starts.

## Verification forks

1. The main model sees the entire conversation along the active path. Its system prompt asks it to emit `<verify>specific claim</verify>` before relying on an unchecked assumption about a file, API, or config key.
2. After the model reply completes, Reflect extracts distinct nonempty claims from its visible text. Tool calls accompanying a tag receive a deferred observation. The model can reissue a call after seeing the verdict.
3. Reflect forks from the latest main node and adds an instruction naming the claim. Each verifier receives the conversation up to the fork point with a verification-specific system prompt. Multiple claims in one reply start concurrently. `llama-server` schedules them on its available slots.
4. Each verifier gets up to three model requests. It can locate evidence with `read_file` or `grep_search`. Reflect also checks the tool name before dispatch, so any attempted shell command or write stays unexecuted.
5. Once a lookup succeeds, a verdict request contains the claim and the fresh workspace evidence. It uses `llama-server`'s JSON response format to request an explanation and a status of `supported`, `contradicted`, or `insufficient`. Reflect displays these as `VERIFIED`, `FALSE ASSUMPTION`, and `UNVERIFIED`. A useful explanation cites the file and line. If the request budget runs out or the server fails to provide a usable verdict, Reflect passes back `UNVERIFIED`.
6. Reflect adds the verdicts to the main conversation and requests the next main response. Any further edits or commands run through the main branch's regular tool loop.

The verdict records the model's interpretation. The saved tool observation gives reviewers access to the evidence behind it. Live tests with a small model showed that a correct file read can still lead to an incorrect verdict label.

Verification tests can exercise a claim that changes after an edit. Start with `status.txt` containing `old_ready`, check the claim with a tag, and let the main branch replace the value. Resume the session and check the old claim again. In the saved tree, the initial verdict should precede the edit, and the later verifier should report the old claim as false.

## Strategic reflection

Reflect checks up to four actual main-branch tool results from the current task. It starts a reflection batch when it observes one of these patterns.

- Two consecutive failures for the same action, or the same failure from two calls to the same tool.
- Two writes to the same file that leave its bytes unchanged.
- Three calls with the same action and unchanged results, including a command that repeatedly exits successfully with the same output.
- Three changes to the same file without a successful Bash result between them.

Each reviewer receives the main conversation up to the stall and the reason it was called. The three reviewers check assumptions, look for project patterns and skills, and propose a simpler route with a visible progress check. Each gets up to three model requests and can use `read_file` and `grep_search`. Reflect saves their branches and summarizes their proposals in the main conversation. The main branch chooses an action and runs any changes or tests. Three parallel slots let the server work on all three requests at once.

`/reflect` starts the same reviewers on the current conversation on demand. It records their proposals and waits for the next task. Automatic reflection pacing applies to detections from tool results.

The Existing patterns reviewer gets up to six recent Git commit subjects when Git and history are present. It opens an explicitly mentioned project skill or the workspace's sole skill as a starting point. These sources can point to a working baseline. Current tool results drive reflection pacing.

### Reflection pacing

Reflect adapts to observed results during the session. After each reflection batch, it waits for at least two new tool results before starting another. Further batches without a recovery raise that interval to four and then eight results. The delay resets if a recently failing command passes or a file that recently stayed unchanged finally changes. Reflect also suppresses repeat reviews for the same pattern within a task. A new user prompt starts a fresh observation window, and the pacing is saved with the session so `--resume` carries it forward.

A single failed attempt gives the model room to recover. The throttle uses observed results across model sizes. Repeated output is a signal to investigate, and a reviewer can propose a direct check on the artifact or milestone if the shell output alone gives little evidence of progress.

If a reviewer discovers a useful tool, the main branch can try it and record the successful command for future work. [Project skills](project-skills.md)

The project retrospectives add a useful question for reviewers. Check whether the expected milestone or artifact advanced even when the command reports success. Match the scenario and environment when comparing results. [Lessons from two development retrospectives](retrospective-lessons.md)

### Skills and Strategy

The CLI shows a **Skills** block when a note is read or saved. A **Strategy** block shows why reflection started, the proposals that came back, and the main-branch tool attempts that follow. Reflect records those attempts and their results in `.refl/strategies/<session-id>.md`. The session tree keeps the full tool observations. A future findings view can follow an action to its observed outcome and link each entry back to the relevant command, result, or file.

- **x86 disassembler found.** Show the executable and the successful discovery or version check. If the project installed it, link to the install result and the follow-up check.
- **Assembler tutorial saved.** Show the source and local path, and link to the tool result that confirmed the saved file could be read.
- **DSP ↔ CPU communication moved from I/O to SSI/DMA.** Link to the implementation and the check of the new path. Include a before-and-after timing result when both runs used the same workload.
- **Workflow saved as a project skill.** Link to the skill and the command and input that demonstrated it.

An entry can start with the current attempt and update as evidence arrives. A result that changes the next action can link to the strategy decision and its rationale. A result that improves a repeatable workflow can link to the skill it creates or updates. The tree holds the detailed investigation and the skill file holds the steps worth reusing.
