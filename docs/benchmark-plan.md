# Benchmark plan for the first release

Once the first release is ready, use a paired evaluation to measure whether verification and strategic reflection help a local model finish more tasks, ground its claims, or recover from stalls. Measure the extra requests, tokens, and time alongside those outcomes.

## Local reflection check

`bench/reflection_ab.py` runs a small paired test against a running `llama-server` before the larger study. It creates each task in a fresh workspace outside the repository and runs it in the `linear` and `reflect` arms, and optionally in `full`. Four tasks contain traps that tend to stall a coding loop: a test that imports a stale vendored module, a build cache that skips rendering, a Latin-1 file that the UTF-8 file tools reject, and an exponential recursion that tuning cannot make fast enough. The last one is an approach stall, where the cause is known but the chosen route is a dead end. A control task has no trap. A deterministic check grades each workspace.

The linear and reflect arms share the system prompt, tools, task prompt, and request budget, so they differ only in whether stall detection can start reflection batches. The trajectories still differ from the first reply onward. In a first run with a 27B model on a single-slot server at temperature 0, every pair diverged at its first model reply, likely because prompt-cache reuse changes the numerics. Use `--repeat` and compare counts rather than single pairs.

That run also showed that a capable model may never stall on these tasks, so reflection never starts and the test measures only its idle cost. `--seeded` starts each run from a scripted stall on the main branch, such as two correct edits to `app.py` followed by the same test failure three times. The tool results in the stall are real. The reflect arm then starts a reflection batch, the linear arm continues without one, and both get the same request budget, which also covers the reviewers. This measures whether reflection helps the model leave a stall it is already in. `--heat` sets the reflection temperature for the seeded stall, so a single stall can exercise the lateral roles. `--thinking` passes the thinking mode through, so the same model can be tested with and without its thinking step, or with thinking only on the main branch.

After each reflection the script asks the model to group proposals that recommend the same next action and records the number of distinct ideas. A word-overlap score rated three reworded copies of one idea as fairly diverse, so the count comes from the model. `--judge RESULTS_DIR` adds the count to an earlier run. The script records requests per branch, prompt tokens the server processed, completion tokens, and wall-clock time, and leaves the judge requests out of those totals. It also replays the stall detector over each main branch to show where the linear arm would have reflected. It alternates the arm order across tasks because the server's prompt cache speeds up whichever arm runs second.

Run `python bench/reflection_ab.py --validate` to confirm that each fixture fails as written, passes with its reference fix, and that each scripted stall triggers the detector on its last step. A few runs per task check the mechanism and its cost. They cannot establish an effect size.

## Benchmark sequence

1. **Check the runner on a handful of Polyglot tasks.** Use a fixed sample of the Exercism exercises and their tests from Aider's benchmark. Record Reflect's own setup and grading. Reproduce Aider's published protocol for any direct score comparison with its leaderboard. [Aider Polyglot benchmark](https://aider.chat/docs/leaderboards/)
2. **Run the first paired study on 30–50 preselected tasks.** Use a pinned Terminal-Bench release for terminal workflows and recovery from environment problems, or SWE-bench Lite for real repository fixes. Pick one suite for the first study. Freeze the task IDs before running either arm and label the result as a subset score. [Terminal-Bench run guide](https://www.tbench.ai/run), [SWE-bench datasets and evaluation](https://github.com/SWE-bench/SWE-bench)
3. **Expand after the pilot.** Keep the tuning tasks out of any later held-out comparison. Use additional task IDs or SWE-bench Verified when there is enough time to run and grade the larger set. Repeat on a second model if the first study warrants checking whether model capability changes the result. [SWE-bench datasets](https://www.swebench.com/)

Run the upstream grader on a known reference solution before trusting the adapter. Run each arm in a clean copy of the task environment and collect the resulting patch or task artifacts for that grader. Keep benchmark runners and container setup outside Reflect's runtime dependencies.

## Paired experiment

| Arm | Configuration |
| --- | --- |
| Linear | Run with `--linear`. Reflect uses its main tool loop, omits the verification instruction, and skips both kinds of forks. |
| Reflection only | Run with `--no-verify`. The system prompt matches the linear arm, and stall detection can start reflection batches. |
| Forks | The same main loop with verification and strategic reflection enabled. |

Pin the Reflect revision, GGUF hash and quantization, llama.cpp build, chat template, sampling settings, hardware, and effective context per slot. Give both arms the same task prompt, main tools, permissions, and initial project guidance and skills. Start a fresh session for each task so learned skills and pacing from prior tasks stay out of the comparison. Record the run order, use the same cache policy, and alternate which arm runs first across tasks.

Use the same server slot configuration for the primary comparison. The linear arm issues one request at a time and the full arm can fill multiple slots. Verify the actual context allowance per slot on the selected build. A separate timing run can compare a one-slot deployment with a multi-slot deployment. If the combined setup helps, use smaller follow-up runs with one type of fork enabled or with forks executed serially to identify what contributed. [llama.cpp server options](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

Set a common wall-clock deadline per task. Record main-loop limits and total model use across **all** branches. A later equal-token comparison can cap aggregate generated tokens, including the forks, to measure how the same inference budget is spent.

## What to record

| Measure | How to count |
| --- | --- |
| Resolve rate | Use the upstream grader. Report paired wins and losses, both-solved and neither-solved counts, and the denominator. Record infrastructure failures separately from agent timeouts and grading failures. |
| Unsupported file or API claims | Count factual claims or actions that rely on a file, symbol, or API absent from the task environment at the time. Use saved transcripts and repository evidence. Treat exploratory searches as attempts. Record incorrect verifier verdicts separately. |
| Wall-clock time | Measure from task submission to final artifact or timeout, including tools and forks. Report median and p90 and count timeouts. Track grader time separately. |
| Inference and interventions | Record prompt and completion tokens, main and fork requests, verifier and reviewer activations, and context-limit failures. Check whether each reflection was followed by a concrete change in the task's expected output or check. |

Keep the task manifest, run settings, grader outputs, and per-task results together. A first report should show where the paired results improved, regressed, or stayed inconclusive under the recorded budget.

Treat a Pi or Claude Code comparison as a separate product comparison. Use the same model and tasks wherever the products support that setup, then measure the actual prompt overhead, tool behavior, success rate, and latency. Record the model and harness together when they must run as a bundle.
