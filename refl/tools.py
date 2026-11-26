import base64
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from itertools import islice
from pathlib import Path


_SPECS = {
    "read_file": (
        "Read numbered lines of a UTF-8 file, as many whole lines as fit in about 12,000 characters. Line "
        "numbers are 1-based and inclusive. A result that stops early says where to continue.",
        {"path": "string", "start_line": "integer", "end_line": "integer"}, ["path"],
    ),
    "grep_search": (
        "Find up to 30 matching lines for a regex in a workspace file or directory.",
        {"pattern": "string", "path": "string"}, ["pattern", "path"],
    ),
    "view_image": (
        "Look at a PNG, JPEG, GIF, or BMP image in the workspace. The image follows the tool result. Convert other "
        "formats, such as SVG, to PNG with bash first.",
        {"path": "string"}, ["path"],
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
# Folders that hold no documents of the project itself, or too many to list.
_NOT_DOCUMENTS = {"node_modules", "third_party", "vendor", "build", "dist", "venv", "__pycache__", "site-packages"}
# Top-level folders whose files are documents, plain text included, and that come right after the root's own.
_DOCUMENT_FOLDERS = {"docs", "doc", "documentation", "wiki", "notes"}
# Guidance files at the workspace root, in the order they appear in the system prompt.
_GUIDANCE_FILES = ("REFLECT.md", "AGENTS.md", "CLAUDE.md")
# grep_search shows up to this many matching lines, each cut at this many characters, and stops after this
# many seconds.
_MATCH_LIMIT = 30
_COLUMN_LIMIT = 300
_SEARCH_SECONDS = 15
_MORE_MATCHES = "\n[More matches omitted. Narrow the search to continue.]"
# view_image sends at most this many bytes of image data. The session keeps each viewed image.
_IMAGE_LIMIT = 32 * 1024 * 1024
# Basalt refuses images above 64 megapixels. Refusing them here lets the model scale the image down, while an image
# the server rejects would stay in the conversation and fail every later request. 64 million is the lower reading.
_PIXEL_LIMIT = 64_000_000
# JPEG markers that start a frame and carry the image size: SOF0 to SOF15 without DHT, JPG, and DAC.
_JPEG_FRAMES = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _image_info(data):
    """Return the format name, MIME type, width, and height of a PNG, JPEG, GIF, or BMP image, or None.

    llama-server decodes images with stb_image, which reads these formats but not WebP or SVG. The size is None
    when the header does not give it.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return "PNG", "image/png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return "GIF", "image/gif", int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"BM") and len(data) >= 26:
        width = int.from_bytes(data[18:22], "little", signed=True)
        height = int.from_bytes(data[22:26], "little", signed=True)
        return "BMP", "image/bmp", abs(width), abs(height)
    if data.startswith(b"\xff\xd8\xff"):
        index = 2
        while index + 4 <= len(data) and data[index] == 0xFF:
            marker = data[index + 1]
            if marker == 0xFF:  # a fill byte before the marker
                index += 1
                continue
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # markers without a length
                index += 2
                continue
            length = int.from_bytes(data[index + 2:index + 4], "big")
            if marker in _JPEG_FRAMES and index + 9 <= len(data):
                height = int.from_bytes(data[index + 5:index + 7], "big")
                width = int.from_bytes(data[index + 7:index + 9], "big")
                return "JPEG", "image/jpeg", width, height
            index += 2 + length
        return "JPEG", "image/jpeg", None, None
    return None
_LINE_NUMBER = re.compile(r"\s*\d+: ")  # the prefix read_file puts on each line
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


# Bash reads each command from a file instead of a -c argument. On Windows, MSYS2 and Git Bash parse their
# command line with rules of their own that turn \\ into \, so a Python heredoc that wrote "\\n" put a line
# break into a Lua string. The command line also holds 32 KB at most. eval keeps the messages that -c gives,
# such as "bash: line 3: ...", so they do not name the file, and "set --" clears the file's path from $1.
_RUN_SCRIPT = 'eval "set --; $(< "$1")"'


def _descendants(pid):
    """The process IDs started under pid, from the parent IDs that ps lists, or none if ps is unavailable."""
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid="], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 2 and all(field.isdigit() for field in fields):
            children.setdefault(int(fields[1]), []).append(int(fields[0]))
    found, pending = [], [pid]
    while pending:
        for child in children.get(pending.pop(), []):
            found.append(child)
            pending.append(child)
    return found


def _end_process_tree(process):
    """Stop Bash with the programs it started. Ending Bash alone leaves them running, such as an emulator."""
    if os.name == "nt":
        # taskkill follows parent process IDs, so it has to run while Bash is still alive.
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        try:
            subprocess.run([str(taskkill), "/F", "/T", "/PID", str(process.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        for pid in _descendants(process.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    process.kill()
    process.wait()


class WorkspaceTools:
    def __init__(self, root, bash=None):
        self.root = Path(root).resolve()
        executable = _bash_path(bash)
        self.bash_executable = Path(executable).absolute() if executable else None

    def skill_paths(self):
        return [path.relative_to(self.root).as_posix()
                for path in sorted((self.root / ".refl" / "skills").glob("*.md")) if path.is_file()]

    def top_level_entries(self, limit=40):
        """Names at the workspace root, with a trailing slash for directories, for reviewer summaries."""
        try:
            entries = sorted(self.root.iterdir(), key=lambda entry: entry.name.casefold())
        except OSError:
            return []
        names = [entry.name + ("/" if entry.is_dir() else "") for entry in entries
                 if entry.name not in {".git", ".refl"}]
        return names[:limit] + (["..."] if len(names) > limit else [])

    def skill_path(self, path):
        target = Path(os.path.abspath(self.root / path))
        if target.parent == self.root / ".refl" / "skills" and target.suffix == ".md":
            return target.relative_to(self.root).as_posix()
        return None

    def project_guidance(self):
        """The guidance files at the workspace root, each under its own heading, or "".

        REFLECT.md is Reflect's own. AGENTS.md and CLAUDE.md are what other coding agents read: in two game-porting
        projects, AGENTS.md held the mandatory rules, such as which MAME source is authoritative, and Reflect saw
        none of them. A file with the same text as an earlier one, such as a CLAUDE.md copied from AGENTS.md, is
        left out.
        """
        parts, seen = [], set()
        for name in _GUIDANCE_FILES:
            if not (self.root / name).is_file():
                continue
            try:
                with self._file(name).open(encoding="utf-8", errors="replace") as source:
                    content = source.read(_OUTPUT_LIMIT + 1)
            except (OSError, ValueError):
                continue
            if len(content) > _OUTPUT_LIMIT:
                content = content[:_OUTPUT_LIMIT] + f"\n[The rest of {name} is cut. Read it with read_file.]"
            content = content.strip()
            if content and content not in seen:
                seen.add(content)
                parts.append(f"Project guidance from {name}:\n{content}")
        return "\n\n".join(parts)

    def document_paths(self, limit=60, scan=20000):
        """The project's documents for the Bookworm: the root's own first, then those in a documentation folder
        such as docs/, then the rest, shallowest first. Markdown and reStructuredText count everywhere, and plain
        text in a documentation folder too. Hidden folders and folders of other projects' code, such as
        third_party/, are left out. At most `scan` entries are looked at.
        """
        found, seen = [], 0
        for directory, folders, files in os.walk(self.root):
            folders[:] = sorted(name for name in folders if not name.startswith(".") and name not in _NOT_DOCUMENTS)
            relative = Path(directory).relative_to(self.root)
            in_folder = bool(relative.parts) and relative.parts[0].lower() in _DOCUMENT_FOLDERS
            suffixes = (".md", ".rst", ".txt") if in_folder else (".md", ".rst")
            for name in files:
                seen += 1
                if name.lower().endswith(suffixes) and not name.startswith("."):
                    found.append(((relative / name).as_posix(), in_folder))
            if seen > scan:
                break
        found.sort(key=lambda item: (0 if "/" not in item[0] else 1 if item[1] else 2, item[0].count("/"),
                                     item[0].casefold()))
        paths = [path for path, _ in found]
        return paths[:limit] + ([f"... and {len(paths) - limit} more"] if len(paths) > limit else [])

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
        # Whole lines up to the output limit. A fixed limit of 80 lines took 7 of a turn's 20 requests to read
        # a 492-line file, while the context holds far more.
        entries, size, stop = [], 0, None
        with self._file(path).open(encoding="utf-8") as source:
            for number, line in enumerate(islice(source, start_line - 1, end_line), start_line):
                entry = f"{number}: {line.rstrip(chr(13) + chr(10))}"
                if entries and size + len(entry) + 1 > _OUTPUT_LIMIT:
                    stop = number
                    break
                if len(entry) > _OUTPUT_LIMIT:
                    # A single line longer than the limit, such as minified code, is cut so the read moves on.
                    entry = entry[:_OUTPUT_LIMIT] + " [Rest of the line truncated]"
                entries.append(entry)
                size += len(entry) + 1
        output = "\n".join(entries)
        if stop is not None:
            output += f"\n[Output limit reached. Continue from line {stop}.]"
        return {"output": output or "No lines in the requested range."}

    def grep_search(self, path, pattern):
        target = self._path(path)
        if not (target.is_file() or target.is_dir()):
            raise ValueError(f"Expected a file or directory at {path}")
        rg = shutil.which("rg")
        if not rg:
            return self._python_search(target, pattern)
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
            text += _MORE_MATCHES
        return {"output": text}

    def _python_search(self, target, pattern):
        """Search like the ripgrep call above, for workspaces where ripgrep is not installed.

        It skips hidden and binary files, and it leaves out files that .gitignore excludes when the target is in
        a Git repository. The pattern uses Python's regex syntax, which agrees with ripgrep's for common patterns.
        """
        try:
            regex = re.compile(pattern)
        except re.error as error:
            return {"error": f"regex parse error: {error}"}
        deadline = time.monotonic() + _SEARCH_SECONDS
        if target.is_file():
            files, prefix = [target], None
        else:
            files, prefix = self._search_files(target), True
        lines = []
        for file in files:
            if time.monotonic() > deadline:
                text = "\n".join(lines[:_MATCH_LIMIT])
                return {"output": (text + "\n" if text else "") + f"[Search stopped after {_SEARCH_SECONDS} "
                                  f"seconds. Narrow the search to continue.]"}
            try:
                data = file.read_bytes()
            except OSError:
                continue
            name = file.relative_to(self.root).as_posix() + ":" if prefix else ""
            if b"\0" in data:
                # Like ripgrep, report a matching binary file only when it is the search target.
                if not prefix and regex.search(data.decode("utf-8", errors="replace")):
                    return {"output": f'binary file matches (found "\\0" byte around offset {data.index(0)})'}
                continue
            text = data.decode("utf-8", errors="replace").removeprefix("﻿")
            for number, line in enumerate(text.split("\n"), 1):
                line = line.removesuffix("\r")
                if regex.search(line):
                    if len(line) > _COLUMN_LIMIT:
                        line = line[:_COLUMN_LIMIT] + " [... omitted end of long line]"
                    lines.append(f"{name}{number}:{line}")
                    if len(lines) > _MATCH_LIMIT:
                        return {"output": "\n".join(lines[:_MATCH_LIMIT]) + _MORE_MATCHES}
        return {"output": "\n".join(lines) if lines else "No matches."}

    def _search_files(self, directory):
        """List the files a ripgrep search of the directory would read, in path order."""
        git = shutil.which("git")
        if git:
            try:
                result = subprocess.run(
                    [git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                    cwd=directory, stdin=subprocess.DEVNULL, capture_output=True, timeout=_SEARCH_SECONDS,
                )
            except (OSError, subprocess.SubprocessError):
                result = None
            # Outside a Git repository, git exits with an error, and the directory walk below takes over.
            if result is not None and result.returncode == 0:
                names = sorted(set(result.stdout.decode("utf-8", errors="replace").split("\0")) - {""})
                return [directory / name for name in names
                        if not any(part.startswith(".") for part in Path(name).parts)
                        and (directory / name).is_file() and not (directory / name).is_symlink()]
        files = []
        for current, folders, names in os.walk(directory):
            folders[:] = [folder for folder in folders
                          if not folder.startswith(".") and not os.path.islink(os.path.join(current, folder))]
            files.extend(Path(current) / name for name in names
                         if not name.startswith(".") and not os.path.islink(os.path.join(current, name)))
        # The same order as git ls-files gives.
        return sorted(files, key=lambda file: file.relative_to(directory).as_posix())

    def view_image(self, path):
        """Return a description for the model and the image as a data URL, which the engine attaches."""
        target = self._file(path)
        size = target.stat().st_size
        if size > _IMAGE_LIMIT:
            raise ValueError(f"{path} has {size:,} bytes, more than the {_IMAGE_LIMIT // (1024 * 1024)} MiB that "
                             f"view_image sends. Scale it down with bash first.")
        data = target.read_bytes()
        info = _image_info(data)
        if info is None:
            raise ValueError(f"{path} is not a PNG, JPEG, GIF, or BMP image. Convert it to PNG with bash first, "
                             f"for example an SVG with rsvg-convert or ImageMagick if one is installed.")
        kind, mime, width, height = info
        if width and height and width * height > _PIXEL_LIMIT:
            raise ValueError(f"{path} has {width:,} x {height:,} pixels, more than the {_PIXEL_LIMIT // 1_000_000} "
                             f"megapixels that view_image sends. Scale it down with bash first.")
        dimensions = f"{width} x {height} pixels, " if width and height else ""
        relative = target.relative_to(self.root).as_posix()
        return {
            "output": f"Showing {relative} ({kind}, {dimensions}{size:,} bytes). The image follows.",
            "image": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
        }

    def bash(self, command, timeout_seconds=60):
        if not self.bash_executable:
            raise ValueError("Bash was not found. Add it to PATH or pass --bash with its executable path.")
        if not command.strip() or timeout_seconds < 1:
            raise ValueError("Expected a command and a positive timeout")
        env = os.environ.copy()
        # Python on Windows reads files and writes its output in the console's code page, such as cp1252, unless
        # told otherwise. In one session, json.load of a UTF-8 file and a print of "→" both failed that way, and
        # the failure led to a false stall. The user's own settings win.
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if os.name == "nt":
            directory = self.bash_executable.parent
            usr_bin = directory.parent / "usr/bin"
            search_path = env.get("PATH", "").split(os.pathsep)
            for utilities in (usr_bin, directory):
                if utilities.is_dir() and utilities not in map(Path, search_path):
                    search_path.insert(0, str(utilities))
            env["PATH"] = os.pathsep.join(search_path)
        script = tempfile.NamedTemporaryFile(prefix="refl-command-", suffix=".sh", delete=False)
        try:
            with script:
                script.write(command.encode("utf-8"))
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(
                    [self.bash_executable, "--noprofile", "--norc", "-c", _RUN_SCRIPT, "bash",
                     Path(script.name).as_posix()],
                    cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                )
                try:
                    exit_code = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    _end_process_tree(process)
                    return {"error": f"Bash timed out after {timeout_seconds} seconds", "output": _tail(output)}
                except BaseException:
                    _end_process_tree(process)
                    raise
                return {"exit_code": exit_code, "output": _tail(output)}
        finally:
            try:
                os.unlink(script.name)
            except OSError:
                pass

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
        note = ""
        if match is None:
            # Models often copy a block with a different number of blank lines or trailing spaces. Accept a
            # unique match that differs only in those. Indentation must still match.
            lines = [line.rstrip() for line in old_lines if line.strip()]
            pattern = re.compile(r"[ \t]*\r?\n(?:[ \t]*\r?\n)*".join(re.escape(line) for line in lines))
            match = pattern.search(text) if lines else None
            note = " old_text matched after ignoring blank lines and trailing spaces."
        if match is None:
            if all(_LINE_NUMBER.match(line) for line in old_lines if line.strip()):
                raise ValueError("old_text was not found. Leave out the line numbers that read_file adds.")
            raise ValueError("old_text was not found. Read the current file before retrying.")
        if pattern.search(text, match.start() + 1) is not None:
            raise ValueError("old_text matches more than once. Include more surrounding text.")

        newline = "\r\n" if "\r\n" in text else "\n"
        replacement = new_text.replace("\r\n", "\n").replace("\n", newline)
        updated = text[:match.start()] + replacement + text[match.end():]
        if updated == text:
            return {"output": f"No change needed in {path}", "changed": False}
        _replace_file(target, updated.encode("utf-8"), expected=original)
        return {"output": f"Edited {path}.{note}" if note else f"Edited {path}", "changed": True}
