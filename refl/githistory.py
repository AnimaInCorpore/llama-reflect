"""Read-only search of a project's Git history, for the Wise guy and the verifier of its facts.

The Wise guy learns how problems like the current one were solved before: in STEArcade, a commit message often
names the problem, the technique, and the measured result in one line. Searches follow the ancestors of HEAD
only, never other branches, so a workspace checked out at a commit's parent, as in the history benchmark, does
not show that commit. A sibling repository, such as a port of the same games to another machine, can be searched
the same way. Nothing leaves the machine.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

# Commits a search lists, characters a commit shows, and seconds a git command may take.
_SEARCH_LIMIT = 20
_SHOW_LIMIT = 12000
_SECONDS = 20
_COMMIT = re.compile(r"[0-9a-fA-F]{4,40}")
_SEARCHES = ("messages", "changes")

HISTORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_history",
            "description": ("Search the project's Git history for commits like the current problem. in: messages "
                            "searches commit messages, changes searches the added and removed lines of each commit "
                            "with a regex. repo: workspace, or sibling for the sibling project if one is set. Lists "
                            "up to 20 commits, newest first, with date and subject."),
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "in": {"type": "string", "enum": list(_SEARCHES)},
                               "repo": {"type": "string", "enum": ["workspace", "sibling"]}},
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_commit",
            "description": ("Show one commit of the project's Git history: its full message, the files it changed, "
                            "and its diff, up to about 12,000 characters. repo: workspace or sibling."),
            "parameters": {
                "type": "object",
                "properties": {"commit": {"type": "string"},
                               "repo": {"type": "string", "enum": ["workspace", "sibling"]}},
                "required": ["commit"],
                "additionalProperties": False,
            },
        },
    },
]
HISTORY_NAMES = {tool["function"]["name"] for tool in HISTORY_TOOLS}


def is_repository(path):
    git = shutil.which("git")
    if not git or not Path(path).is_dir():
        return False
    result = subprocess.run([git, "-C", str(path), "rev-parse", "--verify", "--quiet", "HEAD"],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=_SECONDS)
    return result.returncode == 0


class History:
    """The history of the workspace and of an optional sibling repository."""

    def __init__(self, root, sibling=None):
        self.repos = {"workspace": Path(root)}
        if sibling:
            self.repos["sibling"] = Path(sibling)

    def _git(self, repo, *args):
        git = shutil.which("git")
        if not git:
            raise ValueError("git is not installed")
        if repo not in self.repos:
            raise ValueError("No sibling repository is set" if repo == "sibling" else f"Unknown repo {repo!r}")
        result = subprocess.run([git, "-C", str(self.repos[repo]), "--no-pager", *args], stdin=subprocess.DEVNULL,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=_SECONDS)
        if result.returncode:
            raise ValueError(result.stderr.strip() or f"git exited with {result.returncode}")
        return result.stdout

    def search_history(self, pattern, kind="messages", repo="workspace"):
        if not pattern.strip():
            raise ValueError("Expected a search pattern")
        if kind not in _SEARCHES:
            raise ValueError(f"in must be one of {', '.join(_SEARCHES)}")
        search = (["--regexp-ignore-case", "--extended-regexp", f"--grep={pattern}"] if kind == "messages"
                  else [f"-G{pattern}"])
        # HEAD and its ancestors only: no other branches, so a checkout at a commit's parent hides that commit.
        output = self._git(repo, "log", "HEAD", "--no-merges", f"-n{_SEARCH_LIMIT + 1}", "--date=short",
                           "--format=%h %ad %s", *search, "--")
        lines = output.strip().splitlines()
        if not lines:
            return {"output": "No commits match."}
        more = "\n[More commits match. Narrow the pattern to see them.]" if len(lines) > _SEARCH_LIMIT else ""
        return {"output": "\n".join(lines[:_SEARCH_LIMIT]) + more}

    def show_commit(self, commit, repo="workspace"):
        if not _COMMIT.fullmatch(commit.strip()):
            raise ValueError("Expected a commit ID of 4 to 40 hexadecimal digits")
        commit = commit.strip()
        # Only commits that HEAD contains, for the same reason as in search_history.
        try:
            self._git(repo, "merge-base", "--is-ancestor", commit, "HEAD")
        except ValueError as error:
            raise ValueError(f"Commit {commit} is not in the history of HEAD"
                             + (f": {error}" if "exited with" not in str(error) else "")) from None
        output = self._git(repo, "show", "--no-color", "--stat", "--patch", "--format=commit %H%nDate: %ad%n%n%B",
                           "--date=short", commit, "--")
        if len(output) > _SHOW_LIMIT:
            output = output[:_SHOW_LIMIT] + "\n[The rest of the diff is cut.]"
        return {"output": output}

    def execute(self, function):
        """Run a history tool call and return its result, or an error."""
        try:
            arguments = json.loads(function["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object")
            repo = str(arguments.get("repo") or "workspace")
            if function["name"] == "search_history":
                return self.search_history(str(arguments["pattern"]), str(arguments.get("in") or "messages"), repo)
            if function["name"] == "show_commit":
                return self.show_commit(str(arguments["commit"]), repo)
            raise ValueError(f"Unknown tool {function['name']!r}")
        except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError) as error:
            return {"error": str(error) or type(error).__name__}
