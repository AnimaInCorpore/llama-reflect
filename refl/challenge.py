"""Critics that look for the reasons a plan will fail before its first file change."""

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
# Requests for one challenge: critics, verifiers, answers, and rebuttals. They do not count toward --max-turns,
# which would leave the task too few requests. A request beyond this stops that step, and the rest goes on.
REQUESTS = 24
# The tools whose first call in a task starts the challenge. Changes made through Bash are not caught.
EDIT_TOOLS = {"write_file", "edit_file"}

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
