import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from itertools import islice
from pathlib import Path


_SPECS = {
    "read_file": (
        "Read up to 80 numbered lines of a UTF-8 file. Line numbers are 1-based and inclusive.",
        {"path": "string", "start_line": "integer", "end_line": "integer"}, ["path"],
    ),
    "grep_search": (
        "Find up to 30 matching lines for a regex in a workspace file or directory.",
        {"pattern": "string", "path": "string"}, ["pattern", "path"],
    ),
    "bash": (
        "Run a Bash command from the workspace root and return its exit code and output. Defaults to a 60-second timeout.",
        {"command": "string", "timeout_seconds": "integer"}, ["command"],
    ),
    "write_file": (
        "Create or replace a UTF-8 file, creating parent directories as needed.",
        {"path": "string", "content": "string"}, ["path", "content"],
    ),
    "edit_file": (
        "Replace one unique occurrence of literal old_text in a UTF-8 file. Read the file before editing.",
        {"path": "string", "old_text": "string", "new_text": "string"},
        ["path", "old_text", "new_text"],
    ),
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {key: {"type": kind} for key, kind in properties.items()},
                "required": required,
                "additionalProperties": False,
            },
        },
    }
    for name, (description, properties, required) in _SPECS.items()
]

_TYPES = {"string": str, "integer": int}
_OUTPUT_LIMIT = 12000
_OTHER_AGENT_FILES = {"agents.md", "agents.override.md", "claude.md", "claude.local.md", ".mcp.json"}
_OTHER_AGENT_DIRS = {".agents", ".claude", ".codex", ".pi"}


def _agent_config(parts):
    parts = [part.casefold() for part in parts]
    for index, part in enumerate(parts[:-1]):
        if part in _OTHER_AGENT_DIRS:
            return parts[index + 1] != "skills" or len(parts) <= index + 2
    return bool(parts) and parts[-1] in _OTHER_AGENT_FILES


def _bash_path(requested):
    if requested:
        return shutil.which(requested)
    bash = shutil.which("bash")
    if os.name != "nt":
        return bash

    system_root = Path(os.environ.get("SystemRoot", "C:/Windows")).resolve()
    # The Windows system Bash launcher enters WSL with a different environment.
    if bash and not Path(bash).resolve().is_relative_to(system_root):
        return bash

    candidates = [Path(os.environ.get("SystemDrive", "C:") + "/msys64/usr/bin/bash.exe")]
    git = shutil.which("git")
    if git:
        candidates.append(Path(git).resolve().parent.parent / "usr/bin/bash.exe")
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if directory := os.environ.get(variable):
            candidates.append(Path(directory) / "Git/usr/bin/bash.exe")
    return next((str(path) for path in candidates if path.is_file()), None)


def _replace_file(path, content, expected=None):
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    if os.name == "nt" and mode is not None and not mode & stat.S_IWRITE:
        raise PermissionError(f"File is read-only: {path}")

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        if mode is not None:
            temporary.chmod(mode)
        if expected is not None and path.read_bytes() != expected:
            raise ValueError("File changed during the edit. Read it again before retrying.")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _tail(stream):
    size = stream.seek(0, os.SEEK_END)
    stream.seek(max(0, size - _OUTPUT_LIMIT))
    prefix = "[Earlier output truncated]\n" if size > _OUTPUT_LIMIT else ""
    return prefix + stream.read().decode("utf-8", errors="replace")


class WorkspaceTools:
    def __init__(self, root, bash=None):
        self.root = Path(root).resolve()
        executable = _bash_path(bash)
        self.bash_executable = Path(executable).absolute() if executable else None

    def skill_paths(self):
        return [path.relative_to(self.root).as_posix()
                for path in sorted((self.root / ".refl" / "skills").glob("*.md")) if path.is_file()]

    def skill_path(self, path):
        target = Path(os.path.abspath(self.root / path))
        if target.parent == self.root / ".refl" / "skills" and target.suffix == ".md":
            return target.relative_to(self.root).as_posix()
        return None

    def project_guidance(self):
        path = self.root / "REFLECT.md"
        if not path.is_file():
            return ""
        with self._file("REFLECT.md").open(encoding="utf-8") as source:
            content = source.read(_OUTPUT_LIMIT + 1)
        if len(content) > _OUTPUT_LIMIT:
            return content[:_OUTPUT_LIMIT] + "\n[Remaining project guidance truncated]"
        return content.strip()

    def recent_commits(self):
        if not (self.root / ".git").exists():
            return ""
        git = shutil.which("git")
        if not git:
            return ""
        try:
            result = subprocess.run(
                [git, "--no-pager", "log", "-6", "--format=%h %s", "--no-show-signature", "--no-notes", "--"],
                cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True,
                encoding="utf-8", errors="replace", timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip()[:2000] if result.returncode == 0 else ""

    def _path(self, path):
        if not path:
            raise ValueError("Expected a workspace path")
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("Path must stay inside the workspace")
        return target

    def _file(self, path):
        target = self._path(path)
        if not target.is_file():
            raise ValueError(f"Expected a regular file at {path}")
        return target

    def _write_target(self, path):
        target = self._path(path)
        if target.exists():
            original = Path(os.path.abspath(self.root / path))
            paths = [target.relative_to(self.root)]
            if original.is_relative_to(self.root):
                paths.append(original.relative_to(self.root))
            if any(_agent_config(relative.parts) for relative in paths):
                raise ValueError(f"Existing agent configuration is protected: {path}")
        return target

    def execute(self, function):
        try:
            name = function["name"]
            if name not in _SPECS:
                raise ValueError(f"Unknown tool {name!r}")
            arguments = json.loads(function["arguments"])
            _, properties, required = _SPECS[name]
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object")
            for key in required:
                if key not in arguments:
                    raise ValueError(f"Missing argument {key!r}")
            for key, value in arguments.items():
                if key not in properties or type(value) is not _TYPES[properties[key]]:
                    raise ValueError(f"Invalid argument {key!r} for {name}")
            return getattr(self, name)(**arguments)
        except (KeyError, OSError, OverflowError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
            return {"error": str(error)}

    def read_file(self, path, start_line=1, end_line=None):
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise ValueError("Expected positive line numbers with start_line <= end_line")
        limit = min(end_line, start_line + 79) if end_line is not None else start_line + 79
        count = limit - start_line + 1
        with self._file(path).open(encoding="utf-8") as source:
            # Read one extra line to tell the model whether the file continues past the limit.
            lines = [line.rstrip("\r\n") for line in islice(source, start_line - 1, limit + 1)]
        more = len(lines) > count
        output = "\n".join(f"{number}: {line}" for number, line in enumerate(lines[:count], start_line))
        if len(output) > _OUTPUT_LIMIT:
            output = output[:_OUTPUT_LIMIT] + "\n[Remaining output truncated]"
        if more and (end_line is None or end_line > limit):
            output += f"\n[Line limit reached. Continue from line {limit + 1}.]"
        return {"output": output or "No lines in the requested range."}

    def grep_search(self, path, pattern):
        target = self._path(path)
        if not (target.is_file() or target.is_dir()):
            raise ValueError(f"Expected a file or directory at {path}")
        rg = shutil.which("rg")
        if not rg:
            raise ValueError("ripgrep (rg) is required for grep_search. Add it to PATH.")
        relative = "./" + target.relative_to(self.root).as_posix()
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(
                [rg, "--no-config", "--no-heading", "--line-number", "--color=never",
                 "--max-count=31", "--max-columns=300", "--max-columns-preview", "--", pattern, relative],
                cwd=self.root, stdin=subprocess.DEVNULL, stdout=output, stderr=errors, timeout=15,
            )
            if result.returncode == 1:
                return {"output": "No matches."}
            if result.returncode:
                return {"error": _tail(errors).strip() or f"rg exited with code {result.returncode}"}
            output.seek(0)
            matches = list(islice(output, 31))
        text = b"".join(matches[:30]).decode("utf-8", errors="replace").rstrip("\r\n")
        if len(matches) > 30:
            text += "\n[More matches omitted. Narrow the search to continue.]"
        return {"output": text}

    def bash(self, command, timeout_seconds=60):
        if not self.bash_executable:
            raise ValueError("Bash was not found. Add it to PATH or pass --bash with its executable path.")
        if not command.strip() or timeout_seconds < 1:
            raise ValueError("Expected a command and a positive timeout")
        env = os.environ.copy()
        if os.name == "nt":
            directory = self.bash_executable.parent
            usr_bin = directory.parent / "usr/bin"
            search_path = env.get("PATH", "").split(os.pathsep)
            for utilities in (usr_bin, directory):
                if utilities.is_dir() and utilities not in map(Path, search_path):
                    search_path.insert(0, str(utilities))
            env["PATH"] = os.pathsep.join(search_path)
        with tempfile.TemporaryFile() as output:
            try:
                result = subprocess.run(
                    [self.bash_executable, "--noprofile", "--norc", "-c", command],
                    cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=output,
                    stderr=subprocess.STDOUT, timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return {"error": f"Bash timed out after {timeout_seconds} seconds", "output": _tail(output)}
            return {"exit_code": result.returncode, "output": _tail(output)}

    def write_file(self, path, content):
        target = self._write_target(path)
        if target.exists() and not target.is_file():
            raise ValueError(f"Expected a regular file at {path}")
        data = content.encode("utf-8")
        if target.is_file() and target.stat().st_size == len(data) and target.read_bytes() == data:
            return {"output": f"No change needed in {path}", "changed": False}
        target.parent.mkdir(parents=True, exist_ok=True)
        _replace_file(target, data)
        return {"output": f"Wrote {path}", "changed": True}

    def edit_file(self, path, old_text, new_text):
        if not old_text:
            raise ValueError("old_text must be nonempty")
        target = self._write_target(path)
        if not target.is_file():
            raise ValueError(f"Expected a regular file at {path}")
        original = target.read_bytes()
        text = original.decode("utf-8")
        old_lines = old_text.replace("\r\n", "\n").split("\n")
        pattern = re.compile(r"\r?\n".join(re.escape(line) for line in old_lines))
        match = pattern.search(text)
        if match is None:
            raise ValueError("old_text was not found. Read the current file before retrying.")
        if pattern.search(text, match.start() + 1) is not None:
            raise ValueError("old_text matches more than once. Include more surrounding text.")

        newline = "\r\n" if "\r\n" in text else "\n"
        replacement = new_text.replace("\r\n", "\n").replace("\n", newline)
        updated = text[:match.start()] + replacement + text[match.end():]
        if updated == text:
            return {"output": f"No change needed in {path}", "changed": False}
        _replace_file(target, updated.encode("utf-8"), expected=original)
        return {"output": f"Edited {path}", "changed": True}
