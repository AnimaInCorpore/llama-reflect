"""Critics that look for the reasons a plan will fail, and for a simpler one, before its first file change."""

from .reflection import Role


CHALLENGE_PROMPT = (
    "You are a read-only critic. The main branch has a plan for the user's request and is about to make its "
    "first file change, which ends the conversation. Assume that the plan will fail and find out why. Look for "
    "evidence with read_file or grep_search before you answer, such as a requirement, a caller, a test, or a "
    "setting that the plan does not account for. Do not assume anything that no tool result shows. Object to "
    "the plan, not to the task."
)
ANSWER_PROMPT = (
    "Name the one most likely reason that the plan fails. In objection, state it. In evidence, give the tool "
    "result that supports it, or say that it is unverified. In check, give a cheap command or read that "
    "confirms or refutes it. In likelihood, rate it high, medium, or low. Keep each field brief."
)
FIELDS = ("objection", "evidence", "check", "likelihood")
LIKELIHOODS = ("high", "medium", "low")
REBUTTAL_PROMPT = (
    "You are a read-only critic. A verifier confirmed your objection to the main branch's plan in the "
    "workspace, and the main branch answered it in its last reply. Judge only whether the answer resolves your "
    "objection: a changed plan that removes the cause, or a reason grounded in the conversation why it does not "
    "apply. A promise to be careful does not resolve it. In reason, say what the answer changes or misses. Set "
    "status to resolved or unresolved."
)
REBUTTAL_SCHEMA = {
    "type": "json_object",
    "schema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "status": {"type": "string", "enum": ["resolved", "unresolved"]},
        },
        "required": ["reason", "status"],
        "additionalProperties": False,
    },
}
# The simplifier does not object. It steps back to the goal at the highest level of abstraction and derives the
# simplest strategy from there, without starting from the steps of the current plan.
SIMPLIFY_PROMPT = (
    "You are a read-only critic. The main branch has a plan for the user's request and is about to make its "
    "first file change, which ends the conversation. Step back from the details. First restate the goal at the "
    "highest level of abstraction: what must be true when the task is done, in a sentence or two, without the "
    "steps of the current plan. Then derive the simplest strategy that reaches that goal from the workspace as "
    "it is, such as fewer steps or files, a function, tool, or library that is already available in the "
    "workspace or on this system and does part of the work, or a more direct route. Do not propose a new "
    "dependency. Look for evidence with read_file or grep_search before you answer. The "
    "simpler strategy must keep every requirement of the request and of any spec or tests in the workspace. If "
    "the current plan already follows that strategy or is about as simple, say so."
)
SIMPLIFY_ANSWER_PROMPT = (
    "In goal, restate what must be true when the task is done, without any steps. In simpler_plan, give the "
    "simplest strategy that reaches the goal as a few steps. Leave it empty if the current plan already follows "
    "it or is about as simple. In relies_on, name the existing files, functions, or tools in the workspace that "
    "the strategy depends on, or none. In why_simpler, say what it saves compared with the current plan. In "
    "requirements, list every requirement of the request and of any spec or tests, and how the strategy keeps "
    "it. Keep each field brief."
)
# The goal comes first, so the model abstracts before it proposes.
SIMPLIFY_FIELDS = ("goal", "simpler_plan", "relies_on", "why_simpler", "requirements")
SIMPLIFY_REBUTTAL_PROMPT = (
    "You are a read-only critic. A verifier confirmed that your simpler strategy keeps the requirements, and the "
    "main branch answered it in its last reply. Judge only whether the answer settles it: switching to the "
    "simpler strategy, or a concrete reason grounded in the conversation why it misses something or costs more. "
    "Preferring the current plan without such a reason does not settle it. In reason, say what the answer "
    "decides or misses. Set status to resolved or unresolved."
)
SIMPLIFIER = Role("Simpler plan", "challenge",
                  "Restate the goal at the highest level of abstraction and find a clearly simpler strategy that "
                  "reaches it and keeps every requirement.",
                  tools=True, brief=False)
# Requests for one challenge: critics, verifiers, answers, and rebuttals. They do not count toward --max-turns,
# which would leave the task too few requests. A request beyond this stops that step, and the rest goes on.
REQUESTS = 30
# The tools whose first call in a task starts the challenge. Changes made through Bash are not caught.
EDIT_TOOLS = {"write_file", "edit_file"}
# An edit of a few lines is cheaper to check by making it than by four critics. In a live test, a one-line edit
# took several minutes of challenge over a trailing newline that the edit kept anyway.
SMALL_EDIT_LINES = 3
SMALL_EDIT_CHARS = 200

# They run at the same time, one per server slot.
CRITICS = (
    Role("Requirements", "challenge",
         "Compare the plan with the user's request and with any spec, tests, or documentation in the workspace. "
         "Find a requirement that the plan misses, misreads, or breaks.",
         tools=True, brief=False),
    Role("Environment", "challenge",
         "Check what the plan assumes about its environment: the files and modules it expects, the interpreter "
         "and tool versions, paths, configuration, and the platform. Find the assumption most likely to be "
         "wrong.",
         tools=True, brief=False),
    Role("Edge cases", "challenge",
         "Find an input, a state, or a caller of the changed code for which the planned change gives a wrong "
         "result or breaks behavior that works today.",
         tools=True, brief=False, temperature=0.3),
)


def small_text(text):
    """Whether a text has at most SMALL_EDIT_LINES lines and SMALL_EDIT_CHARS characters."""
    return isinstance(text, str) and len(text) <= SMALL_EDIT_CHARS and len(text.splitlines()) <= SMALL_EDIT_LINES


def small_edit(name, args, replaced):
    """Whether a write_file or edit_file call changes only a few lines.

    `replaced` is the text that write_file would overwrite, or None for a new file. Writing a few lines over a
    large file is not small, because it removes everything else.
    """
    if not isinstance(args, dict):
        return False
    if name == "edit_file":
        return small_text(args.get("old_text")) and small_text(args.get("new_text"))
    return small_text(args.get("content")) and (replaced is None or small_text(replaced))


def answer_schema():
    properties = {field: {"type": "string"} for field in FIELDS}
    properties["likelihood"] = {"type": "string", "enum": list(LIKELIHOODS)}
    return {
        "type": "json_object",
        "schema": {
            "type": "object",
            "properties": properties,
            "required": list(FIELDS),
            "additionalProperties": False,
        },
    }


def simplify_schema():
    return {
        "type": "json_object",
        "schema": {
            "type": "object",
            "properties": {field: {"type": "string"} for field in SIMPLIFY_FIELDS},
            "required": list(SIMPLIFY_FIELDS),
            "additionalProperties": False,
        },
    }


def _field(answer, name):
    return " ".join(str(answer.get(name, "")).split()) if isinstance(answer, dict) else ""


def simplification_of(answer):
    """The simpler strategy of a structured simplifier answer, or "" if the plan is already simple."""
    plan = _field(answer, "simpler_plan")
    return plan if plan.strip(" .") else ""


def simplification_claim(answer):
    """The facts a simpler strategy rests on, as a claim that a verifier can check in today's workspace.

    Whether the strategy will work is a prediction that no file shows yet. What it relies on and which
    requirements exist can be read now.
    """
    relies_on = _field(answer, "relies_on")
    requirements = _field(answer, "requirements")
    claim = []
    if relies_on.strip(" .").lower() not in ("", "none", "nothing"):
        claim.append(f"The workspace has what this strategy relies on, and it does what the strategy needs: "
                     f"{relies_on}.")
    claim.append(f"The request and any spec or tests in the workspace set no requirement beyond these: "
                 f"{requirements or 'none named'}.")
    return f"{' '.join(claim)} Strategy: {simplification_of(answer)}"


def format_simplification(answer):
    """Turn a structured simplifier answer into one line for the main branch."""
    parts = [f"Goal: {_field(answer, 'goal')}", f"Simpler strategy: {simplification_of(answer)}"]
    for field, label in (("relies_on", "Relies on"), ("why_simpler", "Saves"), ("requirements", "Keeps")):
        value = _field(answer, field)
        if value:
            parts.append(f"{label}: {value}")
    return " ".join(part if part.endswith((".", "?", "!")) else part + "." for part in parts)


def objection_of(answer):
    """The objection of a structured critic answer, or "" without one."""
    objection = " ".join(str(answer.get("objection", "")).split()) if isinstance(answer, dict) else ""
    return objection if objection.strip(" .") else ""


def format_objection(answer):
    """Turn a structured critic answer into one line for the main branch, or "" without an objection."""
    objection = objection_of(answer)
    if not objection:
        return ""
    parts = [objection]
    for field, label in (("evidence", "Evidence"), ("check", "Check")):
        value = " ".join(str(answer.get(field, "")).split())
        if value:
            parts.append(f"{label}: {value}")
    likelihood = answer.get("likelihood")
    prefix = f"({likelihood}) " if likelihood in LIKELIHOODS else ""
    return prefix + " ".join(part if part.endswith((".", "?", "!")) else part + "." for part in parts)
