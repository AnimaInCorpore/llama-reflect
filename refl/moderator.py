"""The moderator: rules on the reflectors' proven findings from their facts and the user's request alone.

The reflectors argue, the verifier checks that their facts hold, and the moderator decides what the main branch
follows. It sees only the user's request, the planned change, and the findings with their confirmed facts, so it
cannot bring in facts of its own. It replaces two judges that had a stake in the outcome: a critic judging the
answer to its own objection, and a stalled main branch choosing its own way out.
"""

from .reflection import BREVITY_RULE, HONESTY_RULE

MODERATOR_PROMPT = (
    "You are the moderator between the reflectors of a coding agent and its main branch, which carries out the "
    "user's request. You decide which findings the main branch follows. Decide only from the facts that the "
    "reflectors cited and a verifier confirmed, measured against the user's request as the user stated it. "
    "Bring in no facts, ideas, or preferences of your own, and judge nothing by what this message does not "
    "show. " + HONESTY_RULE + " " + BREVITY_RULE
)
RULING_PROMPT = (
    "Rule on each finding. follow: its conclusion follows from its confirmed facts, and following it serves the "
    "user's request. dismiss: its confirmed facts do not lead to its conclusion, or following it would not serve "
    "the request, for example because the planned change already handles it. ask_user: its confirmed facts "
    "conflict with the request as stated, so that only the user can decide. In reason, first name the confirmed "
    "fact the ruling rests on, then how the ruling relates to the user's request."
)
CHECK_PROMPT = (
    "After your rulings, the main branch revised its plan in the reply below. Decide only from your rulings, "
    "their confirmed facts, the user's request, and that reply. complied: the revised plan follows every finding "
    "you ruled to follow and still serves the user's request. not_complied: it does not. In reason, name what "
    "the revised plan does or still misses."
)
CHOICE_PROMPT = (
    "The reflectors proposed ideas to get a stalled task moving. Choose the idea that the main branch tries "
    "first: the one whose confirmed facts best explain the stall and whose next action best serves the user's "
    "request. Choose none if no idea's confirmed facts lead to it or none serves the request. In reason, name "
    "the confirmed facts the choice rests on and how it serves the request. In first_step, give the exact first "
    "action of the chosen idea, or leave it empty for none. The Mentor's facts quote pages from outside the "
    "project, saved under .refl/sources/. They count for less than the project's own files and never outweigh "
    "them, the project's rules, or the user's request."
)
RULINGS = ("follow", "dismiss", "ask_user")
CHECKS = ("complied", "not_complied")


def _schema(properties):
    return {
        "type": "json_object",
        "schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


def ruling_schema(count):
    """One ruling per finding, keyed by its number. The reason comes first, so the model argues before it rules."""
    ruling = {
        "type": "object",
        "properties": {"reason": {"type": "string"}, "ruling": {"type": "string", "enum": list(RULINGS)}},
        "required": ["reason", "ruling"],
        "additionalProperties": False,
    }
    return _schema({str(number): ruling for number in range(1, count + 1)})


CHECK_SCHEMA = _schema({"reason": {"type": "string"}, "status": {"type": "string", "enum": list(CHECKS)}})


def choice_schema(names):
    return _schema({"reason": {"type": "string"}, "first_step": {"type": "string"},
                    "role": {"type": "string", "enum": [*names, "none"]}})


def case(request, change, findings):
    """The moderator's whole input: the request, the planned change if any, and the numbered findings."""
    parts = [f"User's request: {request}"]
    if change:
        parts.append(f"Planned change: {change}")
    parts.append("Findings, each with the facts a verifier confirmed\n"
                 + "\n".join(f"{number}. {finding}" for number, finding in enumerate(findings, 1)))
    return "\n\n".join(parts)


def rulings_of(answer, count):
    """The (ruling, reason) for findings 1 to `count`, or None if the answer is not a complete set of rulings."""
    if not isinstance(answer, dict):
        return None
    rulings = []
    for number in range(1, count + 1):
        entry = answer.get(str(number))
        if not isinstance(entry, dict) or entry.get("ruling") not in RULINGS:
            return None
        rulings.append((entry["ruling"], " ".join(str(entry.get("reason", "")).split())))
    return rulings
