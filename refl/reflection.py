import json
import os
import re
from collections import deque
from dataclasses import dataclass


REFLECTION_PROMPT = (
    "You are a read-only reviewer investigating why a coding task stalled. Use the recent tool results as "
    "evidence and open relevant project skills with read_file. Start with a read_file or grep_search call that "
    "checks something the main branch has not looked at yet, such as a file that the failing command imports "
    "or reads, or the value it actually uses. Check your idea against the task's requirements, such as a spec "
    "or tests in the workspace, and do not propose anything they rule out. Give one concrete next action, the "
    "evidence behind it, and a check the main branch can run to see progress. Keep your proposal brief."
)

FAILURE, NO_PROGRESS, MANUAL = "failure", "no progress", "manual"
_ANY = frozenset({FAILURE, NO_PROGRESS, MANUAL})
PROPOSAL_FIELDS = ("proposal", "basis", "cost", "fails_if")


@dataclass(frozen=True)
class Role:
    """A reviewer role. The group sets when it is used, and fits lists the stall kinds it suits."""

    name: str
    group: str
    focus: str
    fits: frozenset = _ANY
    tools: bool = False  # inspects the workspace with read_file and grep_search
    brief: bool = True  # sees a short summary of the stall instead of the whole conversation
    temperature: float = 0.0
    fields: tuple = PROPOSAL_FIELDS  # answer fields, generated in this order


# Earlier roles in each group take priority when several fit.
ROLES = (
    Role("Existing patterns", "grounded",
         "Find a project skill, commit, or working example that solved a similar step, and propose reusing it.",
         tools=True, brief=False),
    Role("Diagnosis", "grounded",
         "Name two or three competing causes for the stall, the evidence for each, and one cheap check whose "
         "result tells them apart. Propose running that check first.",
         frozenset({FAILURE, NO_PROGRESS}), tools=True, brief=False),
    Role("Observe", "grounded",
         "Find a value or state that the main branch assumes instead of observing. Propose the smallest command "
         "that prints it.",
         frozenset({FAILURE, NO_PROGRESS}), tools=True, brief=False),
    Role("Known alternative", "alternative",
         "Name an established technique or tool that solves this kind of problem differently from the current "
         "approach. Say where it is standard, how to apply it here, and how to check that it is available.",
         frozenset({NO_PROGRESS, MANUAL}), tools=True, temperature=0.3),
    Role("Switch tools", "alternative",
         "Keep the goal and change the instrument. Propose doing the blocked step with a different tool or "
         "language, such as Bash instead of the file tools or Python instead of sed.",
         frozenset({FAILURE}), temperature=0.3),
    Role("Shrink the problem", "alternative",
         "Propose a smaller version of the problem to solve first, such as a minimal reproduction, a single "
         "case, or a bisection over inputs or commits. Put the smaller case in a new file and leave the task's "
         "inputs unchanged.",
         frozenset({FAILURE, NO_PROGRESS}), temperature=0.3),
    Role("Premortem", "alternative",
         "Assume that the approach the main branch keeps trying will fail. Name the most likely reason and the "
         "change that avoids it.",
         frozenset({NO_PROGRESS, MANUAL}), brief=False, temperature=0.3),
    Role("Analogy", "lateral",
         "First restate the stalled problem in abstraction without words from this project's domain: the parts "
         "involved, how they relate, the goal, and what blocks it. Then name a field of engineering or science "
         "where a problem with the same structure is solved routinely. In basis, name that field, map each "
         "abstract part onto it, and name the mechanism that transfers. In proposal, give one concrete step "
         "here that applies the mechanism.",
         temperature=0.8, fields=("abstraction", "basis", "proposal", "cost", "fails_if")),
    Role("Naive shortcut", "lateral",
         "Propose the simplest thing that might work, even without evidence that it will.",
         temperature=0.8),
    Role("Inversion", "lateral",
         "Name a stated or assumed constraint that may be optional, or invert the goal. Say what becomes "
         "possible without it and how to check whether it is really required.",
         temperature=0.8),
    Role("Backtrack", "lateral",
         "Find the earliest decision that led into this dead end. Propose a different route from that point "
         "and the changes to undo.",
         frozenset({FAILURE, NO_PROGRESS}), brief=False, temperature=0.8),
    Role("Ask the user", "lateral",
         "Name the one question for the user whose answer would unblock the task, and what to do for each "
         "likely answer.",
         temperature=0.8),
)

# Role groups for each reflection temperature. Repeated stalls in a task raise the temperature.
TEMPERATURE_SLOTS = (
    ("grounded", "grounded", "alternative"),
    ("grounded", "alternative", "lateral"),
    ("alternative", "lateral", "lateral"),
)
_KINDS = {"failure": FAILURE, "diagnostic": FAILURE, "repeated failure": FAILURE, "stopped": FAILURE,
          "unchanged file": NO_PROGRESS, "unchanged result": NO_PROGRESS, "edits": NO_PROGRESS,
          "cycle": NO_PROGRESS}


def choose_roles(level, kind, used=frozenset(), unavailable=frozenset()):
    """Pick one role per slot of the temperature level, preferring unused roles that fit the stall kind."""
    chosen = []
    for group in TEMPERATURE_SLOTS[min(level, len(TEMPERATURE_SLOTS) - 1)]:
        candidates = [role for role in ROLES
                      if role.group == group and role not in chosen and role.name not in unavailable]
        candidates.sort(key=lambda role: (role.name in used, kind not in role.fits))
        if candidates:
            chosen.append(candidates[0])
    return chosen


_FILE_WRITES = {"write_file", "edit_file"}
_FRACTIONAL = re.compile(r"\d+\.\d+")


def _result_signature(result):
    """Compare results while ignoring fractional numbers such as test durations."""
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
        # Result signatures of each Bash command in the task, to notice edits that go back and forth.
        self.outputs = {}
        self.runs = {}  # how often each Bash command ran in the task
        self.last_bash = None
        self.kind = None  # stall kind of the latest report
        self.batches = 0  # reflection batches since the task started or last made progress
        self.used = set()  # role names used in those batches

    def start_turn(self):
        self.recent.clear()
        self.reported.clear()
        self.failures.clear()
        self.outputs.clear()
        self.runs.clear()
        self.last_bash = None
        self.kind = None
        self.batches = 0
        self.used.clear()

    def plan(self, kind, *, unavailable=frozenset(), manual=False):
        """Return the temperature level and roles for the next reflection batch, and record them."""
        hottest = len(TEMPERATURE_SLOTS) - 1
        level = hottest if manual else min(self.batches, hottest)
        roles = choose_roles(level, kind, self.used, unavailable)
        self.batches += 1
        self.used.update(role.name for role in roles)
        return level, roles

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
        signature = _result_signature(result)
        if observation.failed:
            previous, count = self.failures.get(observation.action, (None, 0))
            self.failures[observation.action] = (signature, count + 1 if previous == signature else 1)
        else:
            self.failures.pop(observation.action, None)
        if name == "bash":
            self.outputs.setdefault(observation.action, []).append(signature)
            self.runs[observation.action] = self.runs.get(observation.action, 0) + 1
            self.last_bash = observation
        self.recent.append(observation)

    def stopped(self):
        """Report a final answer given while a command the main branch ran again still fails."""
        latest = self.last_bash
        if latest is None or not latest.failed or self.runs.get(latest.action, 0) < 2:
            return None
        return self._report(("stopped", latest.action, _result_signature(latest.result)),
                            f"The main branch gave a final answer while {latest.subject} still fails.")

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
        if latest.name == "bash":
            # A command that returns to an earlier result, but not the one just before, shows edits that go
            # back and forth. The exit code does not matter, since `check; cat out` exits successfully.
            history = self.outputs.get(latest.action, [])
            if len(history) >= 4 and history[-1] != history[-2] and history[-1] in history[:-2]:
                reason = self._report(("cycle", latest.action, history[-1]),
                                      f"{latest.subject} returned an earlier result again.")
                if reason:
                    self.outputs[latest.action] = []
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
        self.kind = _KINDS[key[0]]
        delay = min(8, max(2, self.pacing["delay"] * 2))
        self.pacing.update(delay=delay, remaining=delay)
        return reason
