# Benchmark reflection against llama-server

Use this workflow after changing stall detection, reviewer roles, or the instructions that follow a reflection. It runs the same tasks with and without reflection and grades the workspaces.

## Command

Validate the tasks first. This needs no server.

```text
python bench/reflection_ab.py --validate
```

Every task must report `ok`: it fails as written, passes with its reference fix, and its scripted stall triggers the detector on the last step.

Then run the hardest task without thinking. With llama.cpp b12465, a 27B model solves `opaque-report` directly but fails `ledger-report`, so the arms can differ there. Record the llama.cpp build with each result, because an earlier build made the same model stall on `opaque-report`.

```text
python bench/reflection_ab.py --url http://127.0.0.1:8080 --tasks ledger-report --thinking off --max-turns 40 --repeat 3
```

To see what one reviewer role contributes, pin the roles of a seeded reflection:

```text
python bench/reflection_ab.py --url http://127.0.0.1:8080 --seeded --roles Diagnosis Observe Prerequisites --arms reflect --tasks shadowed-module stale-build latin1-config --thinking off
```

A small model such as Gemma 4 E4B also stalls on `shadowed-module`. Run all tasks with thinking off and on, because reflection helped it only with thinking.

```text
python bench/reflection_ab.py --url http://127.0.0.1:8080 --arms linear reflect --thinking off --max-turns 30 --repeat 3
python bench/reflection_ab.py --url http://127.0.0.1:8080 --arms linear reflect --thinking auto --max-tokens 8192 --max-turns 30 --repeat 2
```

## Check

The summary table lists pass or fail, requests per branch, where the stall detector fired, the reviewer roles, and the number of distinct ideas. For a failed `opaque-report` run, `results.json` says how many report lines still differ. Open a run's session file to read the reflection message, the plan, and the actions that followed.

## Limits

Runs vary even at temperature 0, so compare counts over several repeats. Results go to a new folder under the system temp directory unless `--out` names another one. With thinking enabled, raise `--max-tokens`, because one reply can exceed 8192 tokens.
