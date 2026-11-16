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

| Arm | Configuration |
| --- | --- |
| `linear` | No forks, like `refl --linear`. |
| `reflect` | Strategic reflection without verification, like `refl --no-verify`. |
| `full` | Verification and reflection, like plain `refl`. |

The `linear` and `reflect` arms share the system prompt, tools, task prompt, and request budget, so they differ only in whether stall detection can start reflection batches. The server does not repeat a trajectory exactly, even at temperature 0, because prompt-cache reuse changes the numerics. Use `--repeat` and compare counts rather than single pairs. The script alternates the arm order across tasks because the prompt cache also speeds up whichever arm runs second.

A capable model may never stall on a task, so reflection never starts and the run measures only its idle cost. `--seeded` starts each run from a scripted stall on the main branch, such as two correct edits to `app.py` followed by the same test failure three times. The tool results in the stall are real. The reflect arm then starts a reflection batch, and the linear arm continues without one. `--heat` sets the reflection temperature for the seeded stall, so a single stall can exercise the lateral roles. `--thinking` and `--max-turns` work as in `refl`.

The script records requests per branch, prompt tokens the server processed, completion tokens, and wall-clock time. It replays the stall detector over each main branch to show where the linear arm would have reflected. After each reflection it asks the model to group proposals that recommend the same next action and records the number of distinct ideas. A word-overlap score rated three reworded copies of one idea as fairly diverse, so the count comes from the model. Those judge requests stay out of the totals. `--judge RESULTS_DIR` adds the count to an earlier run.

```text
python bench/reflection_ab.py --validate
python bench/reflection_ab.py --url http://127.0.0.1:8080 --repeat 2
python bench/reflection_ab.py --tasks opaque-report --thinking off --max-turns 40 --repeat 3
python bench/reflection_ab.py --seeded --heat 2 --thinking main --arms reflect
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
