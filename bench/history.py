"""Run Reflect on changes from a project's own git history, checked by the project's own tests.

Each task is a commit whose change a test confirms. The workspace is a clone at the commit's parent, the
request is the commit message, and the task is solved when the check command passes with the commit's version
of the test files. That measures Reflect on the kind of work a project such as STEArcade asks for, with the
project's tools, guidance files, and conventions, instead of on exercises.

A task file lists the tasks for one repository:

    {
      "url": "https://github.com/AnimaInCorpore/STEArcade",
      "copy": ["assets/roms"],
      "tasks": [
        {"name": "blittime-refresh-rate", "commit": "9c2c94b",
         "check": "cd tools && python -m unittest test_blittime", "tests": ["tools/test_blittime.py"]}
      ]
    }

`tests` are files from the commit that the workspace gets before the run and again before grading, so the
model sees what is expected and cannot pass by changing them. `hidden` files are written only for grading.
`copy` names untracked inputs, such as ROM sets, that the checks need: they are copied from the local clone,
or linked with --link, which is faster but lets a run change or delete the originals. `prompt` replaces the
commit message, and `timeout` sets the check's seconds (default 300).

Each workspace is a `git clone --shared` in the results folder, so the local clone and its .git stay as they are.

    python bench/history.py candidates --repo PATH
    python bench/history.py validate --repo PATH --tasks bench/history/stearcade.json
    python bench/history.py run --repo PATH --tasks bench/history/stearcade.json --arms linear reflect
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reflection_ab import ARMS, THINKING_MODES, Task, run_arm, summarize, task_env  # noqa: E402
from refl.tools import WorkspaceTools  # noqa: E402

_CHECK_TIMEOUT = 300
_TEST_FILE = re.compile(r"(^|/)(test_\w+|\w+_test)\.py$")
_CODE_FILE = re.compile(r"\.(py|s|i|c|h|cpp|js|ts|lua)$")


def _git(repo, *args, check=True):
    result = subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL, capture_output=True,
                            encoding="utf-8", errors="replace")
    if check and result.returncode:
        raise SystemExit(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout


def load_tasks(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    names = [task["name"] for task in data["tasks"]]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise SystemExit(f"Duplicate task names in {path}: {', '.join(duplicates)}")
    return data


def prompt_of(repo, spec):
    """The request for a task: its own prompt or the commit message, and the check that shows it is done."""
    request = spec.get("prompt") or _git(repo, "log", "-1", "--format=%B", spec["commit"]).strip()
    tests = spec.get("tests") or []
    done = f"The change is done when `{spec['check']}` passes."
    if tests:
        listed = ", ".join(f"`{test}`" for test in tests)
        done += f" The tests in {listed} describe the expected behavior. Do not change them."
    return f"{request}\n\n{done}"


def _write_files(repo, commit, paths, workspace):
    for relative in paths:
        content = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{relative}"], capture_output=True,
                                 stdin=subprocess.DEVNULL).stdout
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _link(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and source.is_dir():
        import _winapi  # A junction needs no symlink privilege on Windows.
        _winapi.CreateJunction(str(source), str(target))
    else:
        target.symlink_to(source, target_is_directory=source.is_dir())


def _unlink_inputs(workspace, paths):
    """Remove links to the local clone's inputs before the workspace is deleted, so the originals stay."""
    for relative in paths:
        target = workspace / relative
        try:
            os.readlink(target)  # raises for anything but a symlink or a junction
        except (OSError, ValueError):
            continue
        try:
            os.unlink(target)
        except OSError:
            os.rmdir(target)  # a directory junction; its target stays


def prepare(repo, data, spec, workspace, link=False):
    """Clone the repository at the commit's parent into `workspace`, with the task's tests and inputs."""
    if workspace.exists():
        raise SystemExit(f"{workspace} exists already")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    _git(workspace.parent, "clone", "--quiet", "--shared", "--no-checkout", str(repo), workspace.name)
    _git(workspace, "checkout", "--quiet", "--detach", f"{spec['commit']}^")
    for relative in data.get("copy") or []:
        source = Path(repo) / relative
        if not source.exists():
            raise SystemExit(f"Input {relative} is missing in {repo}")
        target = workspace / relative
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        if link:
            _link(source, target)
        elif source.is_dir():
            shutil.copytree(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    _write_files(repo, spec["commit"], spec.get("tests") or [], workspace)


def remove(workspace, data):
    _unlink_inputs(workspace, data.get("copy") or [])

    def writable(function, path, _):
        os.chmod(path, 0o700)
        function(path)

    shutil.rmtree(workspace, onerror=writable)


def run_check(workspace, spec):
    """Run the task's check command with Bash in the workspace. Return (passed, summary)."""
    result = WorkspaceTools(workspace).bash(spec["check"], timeout_seconds=spec.get("timeout", _CHECK_TIMEOUT))
    output = " ".join(str(result.get("output", "")).split())
    if "error" in result:
        return False, f"{result['error']} {output[-200:]}".strip()
    if result.get("exit_code") == 0:
        return True, f"passes: {output[-200:]}"
    return False, f"exit code {result.get('exit_code')}: {output[-300:]}"


def make_task(repo, spec):
    """A benchmark task for run_arm. Its workspace is prepared before the run, so it brings no files."""

    def check(workspace):
        _write_files(repo, spec["commit"], [*(spec.get("tests") or []), *(spec.get("hidden") or [])], workspace)
        return run_check(workspace, spec)

    return Task(name=spec["name"], trap=f"Commit {spec['commit']} of the project's history.",
                prompt=prompt_of(repo, spec), files={}, check=check, reference=None,
                env={"PYTHONDONTWRITEBYTECODE": "1"})


def select(data, names):
    specs = {spec["name"]: spec for spec in data["tasks"]}
    unknown = sorted(set(names or []) - set(specs))
    if unknown:
        raise SystemExit(f"Unknown tasks: {', '.join(unknown)}")
    return [specs[name] for name in names] if names else list(specs.values())


def candidates(repo, max_lines=80, max_files=2):
    """Commits that change a few code files together with a test file, newest first, as (sha, subject, files)."""
    log = _git(repo, "log", "--no-merges", "--numstat", "--format=@@%h%x09%s")
    found, commit = [], None
    for line in log.splitlines():
        if line.startswith("@@"):
            sha, _, subject = line[2:].partition("\t")
            commit = (sha, subject, [])
            found.append(commit)
        elif commit is not None and line.count("\t") == 2:
            added, deleted, path = line.split("\t")
            commit[2].append((path, 0 if added == "-" else int(added), 0 if deleted == "-" else int(deleted)))
    result = []
    for sha, subject, files in found:
        code = [entry for entry in files if _CODE_FILE.search(entry[0])]
        tests = [entry for entry in code if _TEST_FILE.search(entry[0])]
        changed = [entry for entry in code if entry not in tests]
        if tests and changed and len(changed) <= max_files and sum(a + d for _, a, d in changed) <= max_lines:
            result.append((sha, subject, code))
    return result


def validate(repo, data, specs, out, link=False):
    """Check that each task's check fails at the commit's parent with its tests and passes at the commit."""
    ok = True
    for spec in specs:
        workspace = out / f"validate-{spec['name']}"
        try:
            prepare(repo, data, spec, workspace, link)
            with task_env(make_task(repo, spec)):
                _write_files(repo, spec["commit"], spec.get("hidden") or [], workspace)
                before = run_check(workspace, spec)
                _git(workspace, "checkout", "--quiet", "--force", "--detach", spec["commit"])
                after = run_check(workspace, spec)
        finally:
            if workspace.exists():
                remove(workspace, data)
        status = "ok" if not before[0] and after[0] else "BROKEN"
        ok &= status == "ok"
        print(f"{spec['name']}: {status}\n  parent: {before[1][:200]}\n  commit: {after[1][:200]}", flush=True)
    return 0 if ok else 1


async def run(args, repo, data, specs, out):
    print(f"Results: {out}\nTasks: {', '.join(spec['name'] for spec in specs)}", flush=True)
    results = []
    for repeat in range(args.repeat):
        for index, spec in enumerate(specs):
            task = make_task(repo, spec)
            # Alternate the arm order so the server's prompt cache does not always favor the same arm.
            arms = args.arms if (index + repeat) % 2 == 0 else list(reversed(args.arms))
            for arm in arms:
                workspace = out / "workspaces" / f"{spec['name']}-{arm}-{repeat + 1}"
                print(f"Running {spec['name']} with {arm} (repeat {repeat + 1})", flush=True)
                prepare(repo, data, spec, workspace, args.link)
                try:
                    with task_env(task):
                        result = await run_arm(task, arm, workspace, args)
                    result["repeat"] = repeat + 1
                    result["commit"] = spec["commit"]
                    result["diff"] = _git(workspace, "diff", "--stat", check=False).strip()[-2000:]
                    results.append(result)
                    print(f"  {'PASS' if result['passed'] else 'FAIL'} {result['check'][:80]} "
                          f"in {result['seconds']} s, requests {result['requests']}", flush=True)
                finally:
                    if not args.keep:
                        (out / "sessions").mkdir(exist_ok=True)
                        session = workspace / ".refl" / "sessions"
                        if session.is_dir():
                            shutil.copytree(session, out / "sessions" / workspace.name, dirs_exist_ok=True)
                        remove(workspace, data)
                (out / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False),
                                                  encoding="utf-8")
    totals = "\n".join(
        f"- {arm}: {sum(run['passed'] for run in results if run['arm'] == arm)} of "
        f"{sum(run['arm'] == arm for run in results)} solved" for arm in args.arms)
    summary = summarize(results, args.arms) + "\n" + totals + "\n"
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary, flush=True)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["candidates", "validate", "run"])
    parser.add_argument("--repo", required=True, help="local clone of the project")
    parser.add_argument("--tasks", help="task file (validate, run)")
    parser.add_argument("--names", nargs="+", help="tasks to use (default: all)")
    parser.add_argument("--max-lines", type=int, default=80, help="candidates: changed code lines (default: 80)")
    parser.add_argument("--link", action="store_true",
                        help="link the inputs listed under copy instead of copying them; a run can then change them")
    parser.add_argument("--keep", action="store_true", help="keep the workspaces after a run")
    parser.add_argument("--url", default=os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--model", help="model ID (default: first ID from /v1/models)")
    parser.add_argument("--arms", nargs="+", choices=list(ARMS), default=["linear", "reflect"])
    parser.add_argument("--repeat", type=int, default=1, help="runs per task and arm (default: 1)")
    parser.add_argument("--max-turns", type=int, default=40, help="request budget per run (default: 40)")
    parser.add_argument("--max-tokens", type=int, default=32768, help="output tokens per request (default: 32768)")
    parser.add_argument("--timeout", type=int, default=1200, help="seconds per model request (default: 1200)")
    parser.add_argument("--deadline", type=int, default=3600, help="seconds per run (default: 3600)")
    parser.add_argument("--thinking", choices=THINKING_MODES, default="auto",
                        help="thinking mode as in refl --thinking (default: auto)")
    parser.add_argument("--thinking-budget", type=int, metavar="N",
                        help="thinking tokens per request as in refl --thinking-budget (default: as in refl)")
    parser.add_argument("--fork-effort", metavar="LEVEL",
                        help="reasoning effort for the forks as in refl --fork-effort (default: the template's)")
    parser.add_argument("--out", help="results directory (default: a new folder under the system temp directory)")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    if args.command == "candidates":
        for sha, subject, files in candidates(repo, args.max_lines):
            print(f"{sha} {subject[:100]}\n    " + ", ".join(f"{path} +{a}-{d}" for path, a, d in files))
        return 0
    if not args.tasks:
        parser.error(f"{args.command} needs --tasks")
    data = load_tasks(args.tasks)
    specs = select(data, args.names)
    out = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "refl-history" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    out.mkdir(parents=True, exist_ok=True)
    if args.command == "validate":
        return validate(repo, data, specs, out, args.link)
    # run_arm also serves the reflection benchmark's seeded mode, which this adapter does not use.
    args.seeded, args.heat, args.roles = False, 0, None
    asyncio.run(run(args, repo, data, specs, out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
