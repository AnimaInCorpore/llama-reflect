"""A live status panel at the bottom of the terminal while a task runs.

The header line shows the phase, such as the plan check, how many reflectors and verifiers have finished, the
server's busy slots, this task's tokens and current tokens per second, and the times. Below it, one line per
reflector, the moderator, and the main branch says what each does now, and, while its reply streams, whether it
thinks or writes, its tokens, its tokens per second, and its latest words. Work in progress is in italics, proven
results in green, refuted ones in red, and waiting or empty-handed ones dim. Every other line is printed above
the panel. There is no forecast of when a reply ends: its length cannot be known in advance.
"""

import asyncio
import os
import shutil
import time

from . import style

# The panel keeps the bottom rows of the terminal with a scroll region, so other output scrolls above it and
# the panel is drawn at fixed rows. Clearing by moving the cursor up as many lines as were drawn left debris
# whenever a line wrapped.
_SAVE, _RESTORE = "\x1b7", "\x1b8"
_CLEAR = "\x1b[2K"
# Most lines the panel takes below the output, header included.
_MOST_LINES = 10
_TONES = {"wait": "2", "work": "3;36", "good": "32", "bad": "31", "quiet": "2"}
# Symbols per tone, and plain ones for a terminal whose encoding has no such characters.
_GLYPHS = {"header": "◆", "wait": "○", "work": "◐", "good": "●", "bad": "✗", "quiet": "–"}
_PLAIN = {"header": "»", "wait": "·", "work": "»", "good": "+", "bad": "x", "quiet": "-"}


def _clock(seconds):
    seconds = max(0, round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def wanted(stream, environ=None):
    """Whether `stream` is a terminal that can redraw lines. NO_COLOR turns off colors, not the panel."""
    environ = os.environ if environ is None else environ
    return style.wants_color(stream, "auto", environ={"TERM": environ.get("TERM", "")})


def _glyphs(stream):
    try:
        "".join(_GLYPHS.values()).encode(getattr(stream, "encoding", None) or "ascii")
    except (LookupError, UnicodeEncodeError):
        return _PLAIN
    return _GLYPHS


class StatusBar:
    """Keep a live panel below the output of a running task."""

    def __init__(self, stream, enabled, styled=False):
        self.stream = stream
        self.enabled = enabled
        self.styled = styled
        self.glyphs = _glyphs(stream)
        self.phase = None
        self.counts = {}
        self.actors = {}
        self.started = self.phase_started = None
        # Rows kept at the bottom for the panel, and the terminal height they were kept for.
        self.shown = 0
        self.height = 0
        # Busy and all slots from /slots, which counts other clients' requests too.
        self.busy = self.total = 0
        # Each actor's streamed reply: name -> (kind, tokens, tokens per second, latest text). Kind is waiting,
        # thinking, writing, tool, or done.
        self.streams = {}
        self.tokens = 0

    # Progress from the engine.

    def start(self):
        self.started = time.monotonic()
        self.progress(phase="Starting")

    def stop(self):
        self.clear()
        self.phase = None

    def progress(self, **fields):
        """Take a phase, which resets the panel, counts such as critics=(4, 6), actor=(name, doing, tone), and
        stream=(name, kind, tokens, rate, text) for a streaming reply.

        A stream update only records, since one arrives with every token. The poll loop draws twice a second.
        """
        stream = fields.pop("stream", None)
        if stream is not None:
            name, kind, tokens, rate, text = stream
            before = self.streams.get(name)
            # A new request of the same actor starts again at zero.
            self.tokens += tokens - before[1] if before and before[0] != "done" and tokens >= before[1] else tokens
            self.streams[name] = (kind, tokens, rate, text)
            self.actors.setdefault(name, ("", "work"))
            if len(fields) == 0:
                return
        if "phase" in fields:
            self.phase = fields.pop("phase")
            self.phase_started = time.monotonic()
            self.counts, self.actors, self.streams = {}, {}, {}
        actor = fields.pop("actor", None)
        if actor is not None:
            name, doing, tone = actor
            self.actors[name] = (doing, tone)
        for name, value in fields.items():
            if isinstance(value, tuple):
                self.counts[name] = value
        self.draw()

    # The server's load from /slots.

    def observe(self, slots):
        """Count the busy slots. They include other clients' requests, so the panel shows only the count."""
        slots = [slot for slot in slots if isinstance(slot, dict)]
        self.busy = sum(slot.get("is_processing") is True for slot in slots)
        self.total = len(slots)

    def rate(self):
        """Tokens per second of this task's replies that stream now."""
        return sum(rate for kind, _, rate, _ in self.streams.values() if kind in ("thinking", "writing", "tool"))

    async def poll(self, fetch, interval=0.5):
        """Ask `fetch` for /slots every `interval` seconds and redraw, until cancelled."""
        while True:
            try:
                slots = await fetch()
            except Exception:  # A server without /slots still gets a ticking clock.
                slots = None
            if isinstance(slots, list):
                self.observe(slots)
            self.draw()
            await asyncio.sleep(interval)

    # Drawing.

    def lines(self, width=None, height=None):
        """The panel's lines as (text, style) pairs, fitted to the terminal."""
        size = shutil.get_terminal_size((100, 24))
        width, height = width or size.columns, height or size.lines
        now = time.monotonic()
        parts = [f"{self.glyphs['header']} {self.phase} {_clock(now - self.phase_started)}"]
        parts += [f"{name} {done}/{total}" for name, (done, total) in self.counts.items()]
        if self.tokens:
            rate = self.rate()
            parts.append(f"{self.tokens:,} tokens" + (f", {rate:.0f} tok/s" if rate > 0 else ""))
        if self.total:
            parts.append(f"{self.busy}/{self.total} slots busy")
        if self.started is not None:
            parts.append(f"total {_clock(now - self.started)}")
        rows = [(" · ".join(parts), "1;7")]
        room = max(0, min(_MOST_LINES, height - 2) - 1)
        actors = list(self.actors.items())
        if len(actors) > room:
            # The latest changes matter most, so the panel keeps the actors that changed last.
            actors = actors[-room:] if room else []
        span = max((len(name) for name, _ in actors), default=0)
        rows += [(f"  {self.glyphs[tone]} {name.ljust(span)}  " + self._doing(name, doing), _TONES[tone])
                 for name, (doing, tone) in actors]
        # A line wider than the terminal would wrap, and the next redraw could not clear it.
        return [(text if len(text) < width else text[:max(1, width - 2)] + "…", tone) for text, tone in rows]

    def _keep(self, count, height):
        """Keep the bottom `count` rows of a terminal `height` rows tall for the panel."""
        out = []
        if height != self.height and self.shown:
            # The terminal changed size: start over with a full scroll region.
            out.append(f"{_SAVE}\x1b[r{_RESTORE}")
            self.shown = 0
        if count > self.shown:
            # Scroll the output up so that the new rows are free, and put the cursor back on its line.
            grow = count - self.shown
            out.append("\n" * grow + f"\x1b[{grow}A")
        out.append(f"{_SAVE}\x1b[1;{height - count}r{_RESTORE}")
        if count < self.shown:
            # Rows that belonged to the panel are part of the output again, so they start empty.
            out.append(_SAVE + "".join(f"\x1b[{row};1H{_CLEAR}"
                                       for row in range(height - self.shown + 1, height - count + 1)) + _RESTORE)
        self.shown, self.height = count, height
        return "".join(out)

    def _doing(self, name, doing):
        """What an actor does, and how its streaming reply comes along."""
        stream = self.streams.get(name)
        if stream is None:
            return doing
        kind, tokens, rate, text = stream
        if kind == "waiting":
            progress = "waiting for the server"
        elif kind == "done":
            progress = f"{tokens:,} tokens" if tokens else ""
        else:
            label = {"thinking": "thinking", "writing": "writing", "tool": "calling a tool"}[kind]
            progress = f"{label} {tokens:,} tok" + (f", {rate:.0f} tok/s" if rate > 0 else "")
            latest = " ".join(text.split())[-48:]
            if latest:
                progress += f" · …{latest}"
        return " · ".join(part for part in (doing, progress) if part)

    def draw(self):
        if not self.enabled or self.phase is None:
            return
        height = shutil.get_terminal_size((100, 24)).lines
        rows = self.lines(height=height)
        if height - len(rows) < 2:
            return
        out = [self._keep(len(rows), height) if (len(rows), height) != (self.shown, self.height) else ""]
        top = height - len(rows) + 1
        out.append(_SAVE + "".join(
            f"\x1b[{top + index};1H{_CLEAR}" + (f"\x1b[{code}m{line}\x1b[0m" if self.styled else line)
            for index, (line, code) in enumerate(rows)) + _RESTORE)
        self.stream.write("".join(out))
        self.stream.flush()

    def clear(self):
        """Empty the panel's rows and give them back to the output."""
        if self.shown:
            top = self.height - self.shown + 1
            self.stream.write(_SAVE + "".join(f"\x1b[{row};1H{_CLEAR}" for row in range(top, self.height + 1))
                              + "\x1b[r" + _RESTORE)
            self.stream.flush()
            self.shown = 0

    def write(self, text):
        """Print text above the panel. The scroll region keeps it out of the panel's rows."""
        self.stream.write(text + "\n")
        self.stream.flush()
        self.draw()
