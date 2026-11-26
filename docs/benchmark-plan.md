# Benchmark plan

Use paired evaluations to measure whether verification and strategic reflection help a local model finish more tasks, ground its claims, or recover from stalls. Measure the extra requests, tokens, and time alongside those outcomes. The local reflection benchmark in this repository checks the mechanisms and their cost. A larger study on established task suites should follow before any claim about effect sizes.

## Local reflection benchmark

`bench/reflection_ab.py` runs paired tasks against a running `llama-server`. It creates each task in a fresh workspace outside the repository, so Bash commands that the model runs cannot reach this checkout's Git repository. A deterministic check grades each workspace.

| Task | Trap |
| --- | --- |
| `shadowed-module` | A helper puts a stale vendored copy first on the import path, so fixing `app.py` does not change the failure. |
| `stale-build` | The build cache ignores `template.html`, so the build reports success without rendering. |
| `latin1-config` | The file is Latin-1, which the UTF-8 file tools cannot read or edit. |
| `slow-paths` | An exponential recursion that tuning cannot make fast enough. The cause is known, but the chosen route is a dead end. |
| `opaque-report` | `report.py` breaks several rules of `SPEC.md`, some of which defeat the obvious standard-library calls, and the check only compares a hash. Fixing some rules leaves the failure message unchanged. A hidden second dataset catches a hard-coded report, and the grader reports how many lines still differ. |
| `invoice-rates` | A control task without a trap. |
| `invisible-names` | Some customer names are stored decomposed (NFD) in one file and composed (NFC) in the other, so names that print identically, even with `repr()`, do not match. Case and whitespace fixes leave the first failure unchanged. The hidden dataset has Hangul and two different people named René and Rene, so stripping accents fails. |
| `ledger-report` | A longer spec with about a dozen interacting rules behind a hash-only check: a reporting period from a comment line, two date formats, currency codes and decimal commas in prices, composed and decomposed names, display width for East Asian characters, and a total rounded once. Missing any one rule leaves the same failure. |

`invisible-names` and `ledger-report` set `PYTHONIOENCODING=utf-8` for the run. On Windows, Python otherwise writes piped output in the locale encoding, and printing a combining mark fails with an error that names it, which would give the trap away there but not on Linux or macOS.

| Arm | Configuration |
| --- | --- |
| `linear` | No forks, like `refl --linear`. |
| `reflect` | Strategic reflection without verification, like `refl --no-verify`. |
| `full` | Verification and reflection, like plain `refl`. |

The `linear` and `reflect` arms share the system prompt, tools, task prompt, and request budget, so they differ only in whether stall detection can start reflection batches. The server does not repeat a trajectory exactly, even at temperature 0, because prompt-cache reuse changes the numerics. Use `--repeat` and compare counts rather than single pairs. The script alternates the arm order across tasks because the prompt cache also speeds up whichever arm runs second.

A capable model may never stall on a task, so reflection never starts and the run measures only its idle cost. `--seeded` starts each run from a scripted stall on the main branch, such as two correct edits to `app.py` followed by the same test failure three times. The tool results in the stall are real. The reflect arm then starts a reflection batch, and the linear arm continues without one. `--heat` sets the reflection temperature for the seeded stall, so a single stall can exercise the lateral roles. `--roles` names the reviewers for the seeded reflection, which tests a role that would otherwise join only in a later batch, such as Prerequisites. `--thinking` and `--max-turns` work as in `refl`.

The script records requests per branch, prompt tokens the server processed, completion tokens, and wall-clock time. It replays the stall detector over each main branch to show where the linear arm would have reflected. After each reflection it asks the model to group proposals that recommend the same next action and records the number of distinct ideas. A word-overlap score rated three reworded copies of one idea as fairly diverse, so the count comes from the model. Those judge requests stay out of the totals. `--judge RESULTS_DIR` adds the count to an earlier run.

```text
python bench/reflection_ab.py --validate
python bench/reflection_ab.py --url http://127.0.0.1:8080 --repeat 2
python bench/reflection_ab.py --tasks opaque-report --thinking off --max-turns 40 --repeat 3
python bench/reflection_ab.py --seeded --heat 2 --thinking main --arms reflect
python bench/reflection_ab.py --seeded --roles Diagnosis Observe Prerequisites --arms reflect --thinking off
```

`--validate` needs no server. It confirms that each task fails as written, passes with its reference fix, and that each scripted stall triggers the detector on its last step. [Benchmark reflection against llama-server](../.refl/skills/benchmark-reflection.md)

## Results so far

These runs used two 27B models on a single-slot `llama-server` at temperature 0 in November 2026: Qwen3.8-Flash-Next (IQ3_S) and Huihui-Qwen3.8-27B-abliterated (Q4_K_XL). Each configuration ran one to three times, so the results show mechanisms and costs, not effect sizes.

- **Natural runs of the first five tasks.** Across 48 runs on both models, with and without thinking, every run was solved with and without reflection. Neither model stalled, so reflection never started and cost nothing.
- **Seeded stalls.** Both arms recovered in every run. With thinking reviewers, reflection made a run about two to three times as long as continuing without it. `--thinking main` roughly halved the reviewers' time with a similar number of distinct ideas, though some ideas were weaker.
- **Idea diversity.** The three fixed lenses of earlier versions gave one distinct idea in each of six reflections. The role pool gave about two of three. The Analogy role found routes the other roles missed, such as a bottom-up table for `slow-paths`.
- **Root cause or workaround.** On `stale-build`, reflection led to a fix of the cache bug with one model and to the `--force` workaround with the other. The instruction to start with the cheapest idea favored workarounds and was replaced by the evidence-first instruction and the decision step.

`opaque-report` is the first task on which a model stalls by itself. An earlier version with four plain bugs was solved in every run without thinking by rewriting the file from the spec, so the current version adds rules that defeat the obvious library calls. Results with the second model and thinking off:

| Configuration | Requests | Solved | Notes |
| --- | --- | --- | --- |
| Linear | 20 to 40 | 0/8 | The model edits the same function back and forth until the budget runs out. |
| Reflect, before the cycle rule | 20 | 0/3 | The model's check command ends with `cat report.txt`, so it exits successfully and no stall rule fired. |
| Reflect, cheapest idea first | 30 | 0/3 | The Diagnosis reviewer named the cause in every run. The main branch fixed it in two. |
| Reflect with the decision step | 30 | 0/3 | The main branch fixed the cause in every run, and one report line remained wrong. |
| Reflect with the decision step | 40 | 2/2 | Solved in 24 requests each, including the hidden dataset. |

With thinking enabled, one reply of the same model needed more than 8192 output tokens. Raise `--max-tokens` for reasoning models.

These `opaque-report` runs used llama.cpp build b12434. After the message and reflection changes for the 4B model, the second model ran again on build b12465 without thinking. All six tasks were solved in every run of both arms, two repeats each, without a stall, so reflection never started. `opaque-report` took 6 requests. In three more runs without reflection at 40 requests, it was solved every time as well. The model read the spec, the check, the code, and the data in one round, rebuilt the report in an inline script until its hash matched the check, and then rewrote `report.py`. Reflect code from before the changes also solved it on this build in 7 requests in each of three runs, so the change came from the server, not from Reflect. The changes added no cost for this model. The task no longer makes this model stall on this build, so a 27B comparison needs a harder task.

Two harder tasks followed on the same build, without thinking:

- **`invisible-names`.** Solved in all four runs of both arms in 9 requests, without a stall. After the first failure, the model listed the code points of each name, found the combining marks, and normalized the names.
- **`ledger-report`.** No arm solved it reliably. An early version of the spec did not say which Unicode form to show a name in, and a decomposed name then counts 13 columns under the width rule. With that spec, one of three runs without reflection was solved at 30 and at 40 requests, and none with reflection. At 40 requests, all five failed runs differed from the expected report only in that line, so these rounds do not count. With the spec stating the composed form, both arms failed all three runs at 40 requests. Without reflection, every run ended with 4 wrong lines: it padded East Asian names by characters instead of display columns and capitalized `King'S lynn`. With reflection, every run ended with 2 wrong lines: the reflection led to the capitalization fix, and the padding stayed wrong. One run without reflection repeated the same diagnostic command more than 20 times until the budget ran out, which the stall detector would have stopped after three.

In one reflect run, the model rebuilt the revenue sums in an inline script to check `report.py`, left out a row, and then chased a bug in date parsing that did not exist. A rebuilt part needs the same scrutiny as the original.

The first model, Qwen3.8-Flash-Next (IQ3_S), also ran `ledger-report` on build b12465 without thinking at 40 requests. It solved all three runs without reflection in 11 to 14 requests and two of three with reflection in 8 requests each, and no stall rule fired in any run, so reflection never started. The failed run reached the benchmark's 900-second limit after 34 requests with the same two wrong lines as the other model, East Asian names padded by characters. It had spent its last requests writing a new script each time that tried combinations of rounding modes and formats against the hash in `check.py`, and once searched the whole filesystem with `find /`. Every command and result differed, so no rule that looks for repetition could fire. Its last 26 tool results changed no file, while solved runs went at most 10. A rule for 10 such results while a failing command is unresolved now covers this pattern. [Strategic reflection](design.md#strategic-reflection)

The Prerequisites role joins only in a later batch, so seeded runs pinned it next to Diagnosis and Observe with `--roles` on the three tasks whose cause lies outside the edited code. The model solved all six runs, two per task. In `shadowed-module`, Prerequisites proposed `python -c "import app; print(app.__file__)"` to confirm which `app.py` is loaded, and Diagnosis and Observe proposed printing the loaded `slugify` source. In `latin1-config`, the main branch chose Prerequisites' byte-level edit and finished in four requests. In `stale-build`, all three proposed the `--force` workaround. With three grounded roles reading the same file, this model's ideas largely coincided. The 4B model, whose reviewers never looked beyond `app.py` in `shadowed-module`, has not been tested with the new role yet.

### A 4B model

Gemma 4 E4B (UD-Q8_K_XL) ran all six tasks in natural mode with a budget of 30 requests: three repeats without thinking and two with it. Unlike the 27B models, it stalls by itself on three tasks. Each row adds one change to the version above it.

| Version | Thinking | Linear | Reflect |
| --- | --- | --- | --- |
| Before the changes below | off | 12/18 | 9/18 |
| Plain-text tool results and the template check | off | 12/18 | 12/18 |
| Plain-text tool results and the template check | on | 8/12 | 8/12 |
| The stopped rule, required reviewer tool calls, and the decision order | off | 12/18 | 12/18 |
| The tolerant `edit_file` match | off | 12/18 | 12/18 |
| The tolerant `edit_file` match | on | 8/12 | 10/12 |

- **`latin1-config`, `slow-paths`, `invoice-rates`.** Solved in every run of both arms without a stall.
- **`stale-build`.** Before the message changes, the model repeated each call it had announced in text, and its edits of `build.py` contained `\"` copied from the JSON tool results. In the reflect arm, the first reflection found the cache bug, and the failed edits then started a second reflection that lost the finding. After the changes, every run was solved in 8 to 10 requests without a stall. [The coding loop](design.md#the-coding-loop)
- **`shadowed-module`.** Never solved. The model rewrote `slugify` correctly several times and concluded that the environment ran a stale file. It never opened `testsupport.py`, which the test imports first. Before reviewers had to call a tool, they made 8 tool calls in 30 reflections. Afterwards they made 19 in 12, but on this task each one read `app.py` again.
- **`opaque-report`.** With thinking and all changes, the reflect arm solved both runs and the linear arm neither. After three identical failures, the Observe reviewer read `report.py`, pointed out that `capitalize()` turns `NORTH-EAST` into `North-east`, and proposed a one-line command that shows it. The main branch chose that idea, confirmed it, fixed the function, and passed the check, including the hidden dataset. Without thinking, every run ended with one wrong line, `King'S Lynn` from `str.title()`. There the new stopped rule fired in every reflect run, and a reviewer proposed printing the region names, but the main branch never ran `report.py` to look at them. In one earlier run with thinking, the main branch followed a Shrink the problem idea and replaced `sales.csv`, which the task forbids. That role now puts the smaller case in a new file.

Before the stopped rule, 16 of the 20 failed runs with the message changes ended with a final answer right after a failed tool result. The rule turns that answer into a reflection in the reflect arm.

## Polyglot pilot

`bench/polyglot.py` runs the 34 Python exercises of Aider's Polyglot benchmark from a local clone of [Aider-AI/polyglot-benchmark](https://github.com/Aider-AI/polyglot-benchmark). They need only Python's `unittest`. The other languages need Go, Rust, a C++ toolchain, Gradle, or npm packages. Each run gets a fresh workspace with the exercise's stub, its tests, and its instructions from `.docs/`. The reference solution in `.meta/` and the worked approaches in `.approaches/` stay out. The prompt asks for an implementation that passes the tests, keeps the existing names, uses only the standard library, and leaves the tests unchanged. Grading restores the original test files and runs them once more.

Reflect works as an agent here, so it can read and run the tests within its request budget. Aider's published protocol instead gives the model the instructions and the stub, then one more try with the test output. The scores therefore measure Reflect's own setup and are not comparable to Aider's leaderboard.

```text
python bench/polyglot.py --polyglot PATH --validate
python bench/polyglot.py --polyglot PATH --sample 10 --seed 1 --thinking off
```

`--validate` checks that each stub fails its tests and each reference solution passes them. `--sample` picks a fixed random sample, and `--exercises` names exercises explicitly.

The first pilot ran Qwen3.8-Flash-Next (IQ3_S) on llama.cpp b12465 without thinking, with 30 requests per run, on a sample of ten exercises with seed 1: `bottle-song`, `bowling`, `food-chain`, `grep`, `list-ops`, `paasio`, `phone-number`, `proverb`, `scale-generator`, and `two-bucket`. Both arms solved nine of ten, each in 5 to 12 requests without a stall, and the pilot took 23 minutes. Both arms failed `bowling`, whose strike bonuses in the last frame are the hard part. Without reflection, 8 of 31 tests still failed when the budget ran out. With reflection, the detector noticed that the test command returned to an earlier result. The reviewers proposed running the failing tests one at a time to read their real tracebacks, and the run ended with 4 failing tests. The model had piped the final test run through `tail`, so the smoke check reported exit code 0 although tests still failed. One exercise per arm is too little to compare the arms.

All 34 Python exercises then ran once per arm with the same settings. Both arms solved 33 of 34, with 294 requests in 37 minutes without reflection and 276 requests in 35 minutes with it. No reflection started in any run. `bowling` was solved in both arms this time. Both arms failed `pov`, with 10 of 15 tests failing when the budget ran out. In both runs the model wrote its tree rerooting code early and then spent about 20 requests on different inline debugging scripts without changing a file. The wandering rule did not fire because the model ran the tests as `python -m unittest pov_test 2>&1 | tail -5`, which exits successfully, so the detector saw no failing command. Even then, a limit of 15 results would have fired with too few requests left for a reflection. The rule now also reads failure reports in the output and fires after 10 results. Replayed over these runs, it fires in both `pov` runs at request 17 of 30.

A live rerun of `pov` with two runs per arm confirmed that the rule fires, after 17 to 19 tool results, and that the reflect arm reflected once in each run. All four runs still ended with 10 of 15 tests failing. The Diagnosis reviewer recommended replacing the rerooting loop with a fresh implementation built on an undirected adjacency map. The main branch chose that idea but patched the same loop again, as it did with the cheapest ideas in earlier versions.

## History benchmark

Exercises do not show whether Reflect can do the work of a project such as STEArcade, which ports arcade games to the Atari STE with its own translators, a reference machine built from MAME, and mandatory rules in `AGENTS.md`. Of its 676 commits, 576 name a Claude model as co-author. `bench/history.py` turns that history into tasks. A task is a commit whose change a test confirms: the workspace is a `git clone --shared` at the commit's parent, the request is the commit message plus the check command, and the run passes when the check passes with the commit's version of the test files, which grading writes again. The clone lives in the results folder, so the local repository and its `.git` stay as they are. Untracked inputs that the checks need, such as ROM sets, are copied from the local clone, or linked with `--link`, which is faster but lets a run change the originals.

```text
python bench/history.py candidates --repo PATH
python bench/history.py validate --repo PATH --tasks bench/history/stearcade.json
python bench/history.py run --repo PATH --tasks bench/history/stearcade.json --arms linear reflect
```

`candidates` lists commits that change at most two code files, 80 lines by default, together with a test file. `validate` checks that each task's check fails at the parent with the commit's tests and passes at the commit. Most STEArcade candidates are game work whose tests need the ROM set and a build of the game. `bench/history/stearcade.json` starts with one task that needs neither: commit `9c2c94b` infers the desktop's 50, 60, or 71 Hz from a clock calibration in `tools/blittime.py`, with three tests in `tools/test_blittime.py`. At the parent, the tests fail on the missing `clock_calibration`, and at the commit they pass.

## Larger study

### Benchmark sequence

1. **Check the runner on a handful of Polyglot tasks.** Use a fixed sample of the Exercism exercises and their tests from Aider's benchmark. Record Reflect's own setup and grading. Reproduce Aider's published protocol for any direct score comparison with its leaderboard. [Aider Polyglot benchmark](https://aider.chat/docs/leaderboards/)
2. **Run the first paired study on 30–50 preselected tasks.** Use a pinned Terminal-Bench release for terminal workflows and recovery from environment problems, or SWE-bench Lite for real repository fixes. Pick one suite for the first study. Freeze the task IDs before running either arm and label the result as a subset score. [Terminal-Bench run guide](https://www.tbench.ai/run), [SWE-bench datasets and evaluation](https://github.com/SWE-bench/SWE-bench)
3. **Expand after the pilot.** Keep the tuning tasks out of any later held-out comparison. Use additional task IDs or SWE-bench Verified when there is enough time to run and grade the larger set. Repeat on a second model if the first study warrants checking whether model capability changes the result. [SWE-bench datasets](https://www.swebench.com/)

Run the upstream grader on a known reference solution before trusting the adapter. Run each arm in a clean copy of the task environment and collect the resulting patch or task artifacts for that grader. Keep benchmark runners and container setup outside Reflect's runtime dependencies.

### Paired experiment

| Arm | Configuration |
| --- | --- |
| Linear | Run with `--linear`. Reflect uses its main tool loop, omits the verification instruction, and skips both kinds of forks. |
| Reflection only | Run with `--no-verify`. The system prompt matches the linear arm, and stall detection can start reflection batches. |
| Forks | The same main loop with verification and strategic reflection enabled. |

Pin the Reflect revision, GGUF hash and quantization, llama.cpp build, chat template, sampling settings, thinking mode, hardware, and effective context per slot. Give all arms the same task prompt, main tools, permissions, and initial project guidance and skills. Start a fresh session for each task so learned skills and pacing from prior tasks stay out of the comparison. Record the run order, use the same cache policy, and alternate which arm runs first across tasks.

Use the same server slot configuration for the primary comparison. The linear arm issues one request at a time and the other arms can fill multiple slots. Verify the actual context allowance per slot on the selected build. A separate timing run can compare a one-slot deployment with a multi-slot deployment. If the combined setup helps, use smaller follow-up runs with one type of fork enabled or with forks executed serially to identify what contributed. [llama.cpp server options](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

Set a common wall-clock deadline per task. Record main-loop limits and total model use across **all** branches. A later equal-token comparison can cap aggregate generated tokens, including the forks, to measure how the same inference budget is spent.

### What to record

| Measure | How to count |
| --- | --- |
| Resolve rate | Use the upstream grader. Report paired wins and losses, both-solved and neither-solved counts, and the denominator. Record infrastructure failures separately from agent timeouts and grading failures. |
| Unsupported file or API claims | Count factual claims or actions that rely on a file, symbol, or API absent from the task environment at the time. Use saved transcripts and repository evidence. Treat exploratory searches as attempts. Record incorrect verifier verdicts separately. |
| Wall-clock time | Measure from task submission to final artifact or timeout, including tools and forks. Report median and p90 and count timeouts. Track grader time separately. |
| Inference and interventions | Record prompt and completion tokens, main and fork requests, verifier and reviewer activations, and context-limit failures. Check whether each reflection was followed by a concrete change in the task's expected output or check. |

Keep the task manifest, run settings, grader outputs, and per-task results together. A first report should show where the paired results improved, regressed, or stayed inconclusive under the recorded budget.

Treat a Pi or Claude Code comparison as a separate product comparison. Use the same model and tasks wherever the products support that setup, then measure the actual prompt overhead, tool behavior, success rate, and latency. Record the model and harness together when they must run as a bundle.
