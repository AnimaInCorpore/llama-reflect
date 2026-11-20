"""Run Reflect on Python exercises from Aider's Polyglot benchmark, with and without reflection.

The exercises come from a local clone of https://github.com/Aider-AI/polyglot-benchmark, the hardest
Exercism exercises in six languages. This adapter runs the 34 Python exercises, which need only Python's
unittest. Each run gets a fresh workspace outside this repository with the exercise's stub, its tests, and
its instructions. The reference solution in .meta/ and the solution approaches in .approaches/ stay out.

Reflect works as an agent here: it can read and run the tests, and it gets a request budget. That differs
from Aider's published protocol, which gives the model the instructions and the stub, then one more try
with the test output. So the scores measure Reflect's own setup and are not comparable to Aider's
leaderboard. Grading restores the original test files and runs them once more.

    python bench/polyglot.py --polyglot PATH --validate
    python bench/polyglot.py --polyglot PATH --sample 10 --seed 1 --thinking off
    python bench/polyglot.py --polyglot PATH --exercises wordy forth --arms reflect
"""
import argparse
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reflection_ab import ARMS, THINKING_MODES, Task, materialize, run_arm, summarize, task_env  # noqa: E402

_DOCS = (".docs/instructions.md", ".docs/instructions.append.md", ".docs/introduction.md")
_TEST_TIMEOUT = 120


def _exercises(root):
    practice = Path(root) / "python" / "exercises" / "practice"
    if not practice.is_dir():
        raise SystemExit(f"No Python exercises at {practice}. Pass the root of a polyglot-benchmark clone.")
    return {path.name: path for path in sorted(practice.iterdir()) if (path / ".meta" / "config.json").is_file()}


def _run_tests(workspace, modules):
    """Run the exercise's unittest modules in the workspace and summarize the result."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", *modules], cwd=workspace, stdin=subprocess.DEVNULL,
            capture_output=True, encoding="utf-8", errors="replace", timeout=_TEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"the tests did not finish within {_TEST_TIMEOUT} s"
    output = (result.stdout + result.stderr).strip()
    ran = re.search(r"Ran (\d+) tests?", output)
    failed = re.search(r"FAILED \(([^)]*)\)", output)
    if result.returncode == 0:
        return True, f"all {ran.group(1) if ran else '?'} tests pass"
    if ran and failed:
        bad = sum(int(count) for count in re.findall(r"=(\d+)", failed.group(1)))
        return False, f"{bad} of {ran.group(1)} tests fail ({failed.group(1)})"
    return False, "the tests did not run: " + " ".join(output.splitlines()[-3:])[:300]


def exercise_task(name, path):
    files = json.loads((path / ".meta" / "config.json").read_text(encoding="utf-8"))["files"]
    solution, tests = files["solution"][0], files["test"]
    example = (path / files["example"][0]).read_bytes()
    contents = {relative: (path / relative).read_bytes() for relative in [solution, *tests, *_DOCS]
                if (path / relative).is_file()}
    docs = [relative for relative in _DOCS if relative in contents]
    modules = [Path(test).stem for test in tests if Path(test).stem.endswith("_test")]
    command = f"python -m unittest {' '.join(modules)}"
    prompt = (
        f"Solve this exercise. The instructions are in {', '.join(f'`{doc}`' for doc in docs)}. Implement the "
        f"solution in `{solution}` so that `{command}` passes. Keep the names of the existing functions and "
        f"classes, because the tests use them. Use only the Python standard library, and do not modify "
        f"{', '.join(f'`{test}`' for test in tests)}."
    )

    def check(workspace):
        for test in tests:
            (workspace / test).write_bytes(contents[test])
        return _run_tests(workspace, modules)

    return Task(
        name=name,
        trap="An Exercism exercise from Aider's Polyglot benchmark.",
        prompt=prompt,
        files=contents,
        check=check,
        reference=lambda workspace: (workspace / solution).write_bytes(example),
        env={"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )


def select(args):
    exercises = _exercises(args.polyglot)
    if args.exercises:
        unknown = sorted(set(args.exercises) - set(exercises))
        if unknown:
            raise SystemExit(f"Unknown exercises: {', '.join(unknown)}")
        names = args.exercises
    elif args.sample:
        names = sorted(random.Random(args.seed).sample(sorted(exercises), min(args.sample, len(exercises))))
    else:
        names = sorted(exercises)
    return [exercise_task(name, exercises[name]) for name in names]


def validate(tasks):
    """Check that each stub fails its tests and the reference solution passes them."""
    ok = True
    for task in tasks:
        with task_env(task), tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            materialize(task, workspace)
            before = task.check(workspace)
            task.reference(workspace)
            after = task.check(workspace)
        status = "ok" if not before[0] and after[0] else "BROKEN"
        ok &= status == "ok"
        print(f"{task.name}: {status}\n  stub:      {before[1]}\n  reference: {after[1]}", flush=True)
    return 0 if ok else 1


async def run(args, tasks):
    out = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "refl-polyglot" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    out.mkdir(parents=True, exist_ok=True)
    print(f"Results: {out}\nExercises: {', '.join(task.name for task in tasks)}", flush=True)
    results = []
    for repeat in range(args.repeat):
        for index, task in enumerate(tasks):
            # Alternate the arm order so the server's prompt cache does not always favor the same arm.
            arms = args.arms if (index + repeat) % 2 == 0 else list(reversed(args.arms))
            for arm in arms:
                workspace = out / "workspaces" / f"{task.name}-{arm}-{repeat + 1}"
                print(f"Running {task.name} with {arm} (repeat {repeat + 1})", flush=True)
                with task_env(task):
                    result = await run_arm(task, arm, workspace, args)
                result["repeat"] = repeat + 1
                results.append(result)
                print(f"  {'PASS' if result['passed'] else 'FAIL'} {result['check'][:80]} "
                      f"in {result['seconds']} s, requests {result['requests']}", flush=True)
                (out / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False),
                                                  encoding="utf-8")
    totals = "\n".join(
        f"- {arm}: {sum(run['passed'] for run in results if run['arm'] == arm)} of "
        f"{sum(run['arm'] == arm for run in results)} solved" for arm in args.arms)
    summary = summarize(results, args.arms) + "\n" + totals + "\n"
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary, flush=True)


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--polyglot", required=True, help="root of a polyglot-benchmark clone")
    parser.add_argument("--url", default=os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--model", help="model ID (default: first ID from /v1/models)")
    parser.add_argument("--exercises", nargs="+", help="exercise names (default: all, or --sample)")
    parser.add_argument("--sample", type=int, help="run a fixed random sample of this many exercises")
    parser.add_argument("--seed", type=int, default=1, help="seed for --sample (default: 1)")
    parser.add_argument("--arms", nargs="+", choices=list(ARMS), default=["linear", "reflect"])
    parser.add_argument("--repeat", type=int, default=1, help="runs per exercise and arm (default: 1)")
    parser.add_argument("--max-turns", type=int, default=30, help="request budget per run (default: 30)")
    parser.add_argument("--max-tokens", type=int, default=4096, help="output tokens per request (default: 4096)")
    parser.add_argument("--timeout", type=int, default=300, help="seconds per model request (default: 300)")
    parser.add_argument("--deadline", type=int, default=900, help="seconds per run (default: 900)")
    parser.add_argument("--thinking", choices=THINKING_MODES, default="auto",
                        help="thinking mode as in refl --thinking (default: auto)")
    parser.add_argument("--thinking-budget", type=int, metavar="N",
                        help="thinking tokens per request as in refl --thinking-budget (default: as in refl)")
    parser.add_argument("--out", help="results directory (default: a new folder under the system temp directory)")
    parser.add_argument("--validate", action="store_true", help="check the stubs and reference solutions only")
    args = parser.parse_args()
    # run_arm also serves the reflection benchmark's seeded mode, which this adapter does not use.
    args.seeded, args.heat, args.roles = False, 0, None
    tasks = select(args)
    if args.validate:
        return validate(tasks)
    asyncio.run(run(args, tasks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
