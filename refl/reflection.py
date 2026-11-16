import json
import os
import re
from collections import deque
from dataclasses import dataclass


LENSES = (
    ("Assumptions", "Find one assumption that conflicts with the latest tool results. Check it against the workspace."),
    ("Existing patterns", "Read a relevant project skill or working example. Find a proven tool or procedure to reuse."),
    ("Simpler approach", "Propose a smaller route with a check that shows whether the expected result advanced."),
)

REFLECTION_PROMPT = (
    "You are a read-only reviewer investigating why a coding task stalled. Use the recent tool results as "
    "evidence and open relevant project skills with read_file. You can inspect the workspace with read_file "
    "and grep_search. Give one concrete next action, the evidence behind it, and a check the main branch "
    "can run to see progress. Keep your proposal brief."
)

_FILE_WRITES = {"write_file", "edit_file"}
_FRACTIONAL = re.compile(r"\d+\.\d+")


def _failure_signature(result):
    """Compare failures while ignoring fractional numbers such as test durations."""
    output = result.get("output")
    if isinstance(output, str):
        output = _FRACTIONAL.sub("#", output)
    return json.dumps({**result, "output": output}, sort_keys=True)


@dataclass
class _Observation:
    name: str
    arguments: str
    path: str
    subject: str
    result: dict

    @property
    def action(self):
        return self.name, self.arguments

    @property
    def failed(self):
        return "error" in self.result or self.result.get("exit_code", 0) != 0


class StallDetector:
    """Track a bounded window of real main-branch tool results."""

    def __init__(self, pacing):
        self.pacing = pacing
        self.recent = deque(maxlen=4)
        self.reported = set()
        # Failure signatures per action for the whole task, so an edit-and-retest cycle that keeps
        # failing the same way is visible even after it scrolls out of the recent window.
        self.failures = {}

    def start_turn(self):
        self.recent.clear()
        self.reported.clear()
        self.failures.clear()

    def record(self, function, result):
        name = function["name"]
        raw = function["arguments"]
        try:
            arguments = json.loads(raw)
        except ValueError:
            arguments = None
        args = arguments if isinstance(arguments, dict) else {}
        path = args.get("path")
        path = os.path.normcase(os.path.normpath(path)) if isinstance(path, str) and path else ""
        command = args.get("command")
        key = command.strip() if name == "bash" and isinstance(command, str) else json.dumps(arguments, sort_keys=True)
        if arguments is None:
            key = raw
        subject = (command.strip() if isinstance(command, str) else path) or name
        observation = _Observation(name, key, path, subject[:120], result)

        self.pacing["remaining"] = max(0, self.pacing["remaining"] - 1)
        changed = result.get("changed") is True
        passed = name == "bash" and result.get("exit_code") == 0
        recovered = (
            passed and any(prior.failed and prior.action == observation.action for prior in self.recent)
        ) or (
            changed and any(prior.path == path and prior.result.get("changed") is False for prior in self.recent)
        )
        if recovered:
            self.pacing.update(delay=0, remaining=0)
            self.start_turn()
        elif changed:
            self.reported = {key for key in self.reported if key[0] == "edits"}
        elif passed:
            self.reported = {key for key in self.reported if key[0] != "edits"}
        if observation.failed:
            signature = _failure_signature(result)
            previous, count = self.failures.get(observation.action, (None, 0))
            self.failures[observation.action] = (signature, count + 1 if previous == signature else 1)
        else:
            self.failures.pop(observation.action, None)
        self.recent.append(observation)

    def stall(self):
        recent = list(self.recent)
        if len(recent) < 2:
            return None
        latest, previous = recent[-1], recent[-2]
        if latest.failed and previous.failed and latest.name == previous.name:
            if latest.action == previous.action:
                return self._report(("failure", latest.action), f"{latest.subject} failed twice in a row.")
            if latest.result == previous.result:
                diagnostic = json.dumps(latest.result, sort_keys=True)
                return self._report(("diagnostic", latest.name, diagnostic),
                                    f"Two {latest.name} calls produced the same failure.")
        if latest.failed:
            signature, count = self.failures.get(latest.action, (None, 0))
            if count >= 3:
                reason = self._report(("repeated failure", latest.action, signature),
                                      f"{latest.subject} failed the same way three times.")
                if reason:
                    # Require three more identical failures before this pattern triggers again.
                    self.failures[latest.action] = (signature, 0)
                    return reason

        unchanged = 0
        no_op_writes = 0
        edits = 0
        for event in reversed(recent):
            if event.result.get("changed") is True:
                break
            if event.action == latest.action:
                if event.result != latest.result:
                    break
                unchanged += 1
            if event.name in _FILE_WRITES and event.path == latest.path and event.result.get("changed") is False:
                no_op_writes += 1

        if latest.name in _FILE_WRITES and latest.path and latest.result.get("changed") is False and no_op_writes >= 2:
            return self._report(("unchanged file", latest.path), f"Two writes to {latest.subject} left its bytes unchanged.")
        if unchanged >= 3:
            return self._report(("unchanged result", latest.action), f"{latest.subject} returned the same result three times.")

        if latest.name in _FILE_WRITES and latest.path and latest.result.get("changed") is True:
            for event in reversed(recent):
                if event.name == "bash" and event.result.get("exit_code") == 0:
                    break
                if event.name in _FILE_WRITES and event.path == latest.path and event.result.get("changed") is True:
                    edits += 1
            if edits >= 3:
                return self._report(("edits", latest.path),
                                    f"{latest.subject} was changed three times without a successful Bash check.")
        return None

    def _report(self, key, reason):
        if self.pacing["remaining"] or key in self.reported:
            return None
        self.reported.add(key)
        delay = min(8, max(2, self.pacing["delay"] * 2))
        self.pacing.update(delay=delay, remaining=delay)
        return reason
