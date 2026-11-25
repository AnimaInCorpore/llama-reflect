"""Colors and font styles for Reflect's terminal output, with ANSI escape codes and the standard library only.

Each kind of output has its own style, so that the main branch's work, verification, the plan challenge, and
strategic reflection stay apart in a long session. The styles avoid bright white and plain blue, which vanish
on a light or a dark background.
"""

import os
import re
import sys

from .tools import TOOLS

COLOR_MODES = ("auto", "always", "never")

_RESET = "\x1b[0m"
_STYLES = {
    "tool": "1",          # bold: the tool name of a main-branch call
    "answer": "1",        # bold: the final answer
    "verify": "36",       # cyan: verifiers
    "verified": "1;32",   # bold green, red, and yellow: the three verdicts
    "false": "1;31",
    "unverified": "1;33",
    "challenge": "35",    # magenta: the plan challenge, its critics, and the main branch's answers to them
    "reflect": "94",      # bright blue: strategic reflection and its reviewers
    "moderate": "92",     # bright green: the moderator's rulings and choices
    "think": "2;3",       # dim italic: the model's thinking, which reads as an aside
    "quiet": "2",         # dim: skills and status lines
    "warning": "33",
    "error": "31",
}
_TOOL_NAMES = frozenset(tool["function"]["name"] for tool in TOOLS)
_VERDICTS = (("[VERIFIED]", "verified"), ("[FALSE ASSUMPTION]", "false"), ("[UNVERIFIED]", "unverified"))
# A message whose first line is one of these words is a block: a heading and the lines under it.
_HEADINGS = {"Challenge": "challenge", "Strategy": "reflect", "Moderator": "moderate", "Skills": "quiet",
             "Thinking": "think"}
# Without --verbose, thinking shows only its last lines, where the conclusion usually is.
THINKING_LINES = 6
# Earlier prefixes win, so "Context: ... in use" counts as a warning before "Context:" counts as status.
_PREFIXES = (
    ("refl: ", "error"),
    ("[Turn stopped]", "error"),
    ("Verifying ", "verify"),
    ("verify ", "verify"),
    ("Challenging as ", "challenge"),
    ("Challenge ", "challenge"),
    ("challenge ", "challenge"),
    ("Reviewing as ", "reflect"),
    ("Reflection ", "reflect"),
    ("reflect ", "reflect"),
    ("Strategy log unavailable", "warning"),
    ("Thinking stopped", "warning"),
    ("The reply reached", "warning"),
    ("The chat template has no", "warning"),
    ("--fork-effort", "warning"),
)
# One line that sums up each check, plan challenge, and reflection. These show without --verbose.
_SUMMARIES = (("Plan check:", "challenge"), ("Reflection:", "reflect"), ("Check:", "verify"))
# The main branch's words to the user, which read as the conversation rather than as processing.
CHAT_MARK = "» "
# The details behind the summaries, shown only with --verbose.
_DETAILS = ("Challenge\n", "Challenge ", "Challenging as ", "challenge ", "Moderator\n", "Verifying ", "verify ",
            "[VERIFIED]", "[FALSE ASSUMPTION]", "[UNVERIFIED]", "Strategy\n", "Reviewing as ", "Reflection ",
            "reflect ")
_CONTEXT_WARNING = re.compile(r"Context: [\d,]+ of [\d,]+ tokens in use")
# The pipe through which mintty, the terminal of MSYS2 and Git Bash, connects a native program, such as
# \msys-1888ae32e00d56aa-pty0-to-master. An ordinary pipe between two programs has another name.
_MSYS_PTY = re.compile(r"(?:msys|cygwin)-[0-9a-f]+-pty\d+-(?:to|from)-master")
_STATUS = ("Model:", "Workspace:", "Slots:", "Slot status", "Server ", "Context:", "Reasoning effort",
           "Image input", "Session:", "Latest earlier session", "Enter a task", "Conversation switched")


def _wrap(text, style):
    # Each line gets its own codes, so a line that a pager or a log viewer shows alone keeps its style.
    code = f"\x1b[{_STYLES[style]}m"
    return "\n".join(f"{code}{line}{_RESET}" if line else line for line in text.split("\n"))


class Styler:
    """Style Reflect's messages for one output stream, or pass them through unchanged."""

    def __init__(self, enabled):
        self.enabled = enabled

    def answer(self, text):
        return _wrap(text, "answer") if self.enabled else text

    def event(self, message):
        if not self.enabled or not message:
            return message
        if message.startswith(CHAT_MARK):
            # The marker stands out, and the words keep the terminal's own color, like the user's.
            return f"\x1b[1m{CHAT_MARK.strip()}{_RESET} {message[len(CHAT_MARK):]}"
        for label, style in _SUMMARIES:
            if message.startswith(label):
                return f"\x1b[1;{_STYLES[style]}m{label}{_RESET}{_wrap(message[len(label):], style)}"
        heading, newline, body = message.partition("\n")
        if newline and heading in _HEADINGS:
            style = _HEADINGS[heading]
            return f"\x1b[1;{_STYLES[style]}m{heading}{_RESET}\n{_wrap(body, style)}"
        for label, style in _VERDICTS:
            if message.startswith(label):
                return f"\x1b[{_STYLES[style]}m{label}{_RESET}{_wrap(message[len(label):], 'verify')}"
        if _CONTEXT_WARNING.match(message):
            return _wrap(message, "warning")
        for prefix, style in _PREFIXES:
            if message.startswith(prefix):
                return _wrap(message, style)
        if message.startswith(_STATUS):
            return _wrap(message, "quiet")
        name, space, rest = message.partition(" ")
        if name in _TOOL_NAMES:
            return f"\x1b[{_STYLES['tool']}m{name}{_RESET}{space}{rest}"
        return message


def brief(message, lines=THINKING_LINES):
    """Shorten a thinking block to its last lines, with a note on what is left out."""
    if not message.startswith("Thinking\n"):
        return message
    body = [line for line in message[len("Thinking\n"):].splitlines() if line.strip()]
    if len(body) <= lines:
        return "Thinking\n" + "\n".join(body)
    hidden = len(body) - lines
    return (f"Thinking\n... {hidden} earlier line{'s' if hidden > 1 else ''}, --verbose shows all\n"
            + "\n".join(body[-lines:]))


def is_detail(message):
    """Whether a message is a detail behind a summary line, such as one critic's step or one verdict."""
    return message.startswith(_DETAILS)


def wants_color(stream, mode="auto", environ=None):
    """Whether to style `stream`: always, never, or for a terminal without NO_COLOR set."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    environ = os.environ if environ is None else environ
    if environ.get("NO_COLOR") or environ.get("TERM") == "dumb":
        return False
    if os.name == "nt":
        return _windows_terminal(stream)
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _windows_terminal(stream):
    """Whether `stream` is a console that now shows ANSI codes, or the pty of mintty in MSYS2 or Git Bash."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(stream.fileno())
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, ImportError, OSError, ValueError):
        return False
    kernel32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    mode = wintypes.DWORD()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        # Windows 10 and later show ANSI codes once virtual terminal processing is on.
        virtual_terminal = 0x0004
        return bool(mode.value & virtual_terminal) or bool(
            kernel32.SetConsoleMode(handle, mode.value | virtual_terminal))
    return _msys_pty(ctypes, wintypes, kernel32, handle)


def _msys_pty(ctypes, wintypes, kernel32, handle):
    # mintty connects native programs to its terminal through a named pipe instead of a console, so isatty()
    # is False there. The pipe's name tells it apart from an ordinary pipe, as Git for Windows checks too.
    class FileNameInfo(ctypes.Structure):
        _fields_ = (("length", wintypes.DWORD), ("name", wintypes.WCHAR * 512))

    kernel32.GetFileInformationByHandleEx.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                       wintypes.DWORD)
    info = FileNameInfo()
    if not kernel32.GetFileInformationByHandleEx(handle, 2, ctypes.byref(info), ctypes.sizeof(info)):
        return False
    return _MSYS_PTY.search(info.name[:info.length // ctypes.sizeof(wintypes.WCHAR)]) is not None


# Reflect writes its events to stderr and its answers to stdout, each styled only if it reaches a terminal.
err = Styler(False)
out = Styler(False)
# Whether the details behind the summary lines show. Without --verbose, refl hides them.
show_details = True


def configure(mode="auto", details=True):
    global show_details
    err.enabled = wants_color(sys.stderr, mode)
    out.enabled = wants_color(sys.stdout, mode)
    show_details = details
