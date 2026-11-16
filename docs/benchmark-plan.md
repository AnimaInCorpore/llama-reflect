# Benchmark plan for the first release

Once the first release is ready, use a paired evaluation to measure whether verification and strategic reflection help a local model finish more tasks, ground its claims, or recover from stalls. Measure the extra requests, tokens, and time alongside those outcomes.

## Benchmark sequence

1. **Check the runner on a handful of Polyglot tasks.** Use a fixed sample of the Exercism exercises and their tests from Aider's benchmark. Record Reflect's own setup and grading. Reproduce Aider's published protocol for any direct score comparison with its leaderboard. [Aider Polyglot benchmark](https://aider.chat/docs/leaderboards/)
2. **Run the first paired study on 30–50 preselected tasks.** Use a pinned Terminal-Bench release for terminal workflows and recovery from environment problems, or SWE-bench Lite for real repository fixes. Pick one suite for the first study. Freeze the task IDs before running either arm and label the result as a subset score. [Terminal-Bench run guide](https://www.tbench.ai/run), [SWE-bench datasets and evaluation](https://github.com/SWE-bench/SWE-bench)
3. **Expand after the pilot.** Keep the tuning tasks out of any later held-out comparison. Use additional task IDs or SWE-bench Verified when there is enough time to run and grade the larger set. Repeat on a second model if the first study warrants checking whether model capability changes the result. [SWE-bench datasets](https://www.swebench.com/)

Run the upstream grader on a known reference solution before trusting the adapter. Run each arm in a clean copy of the task environment and collect the resulting patch or task artifacts for that grader. Keep benchmark runners and container setup outside Reflect's runtime dependencies.

## Paired experiment

| Arm | Configuration |
| --- | --- |
| Linear | Run with `--linear`. Reflect uses its main tool loop, omits the verification instruction, and skips both kinds of forks. |
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
