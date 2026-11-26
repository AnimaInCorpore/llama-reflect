"""Keeping the main conversation within the server's context.

In a session that ran out of context at 98,398 tokens, the main branch's earlier thinking made up 163,824 of the
286,284 characters sent, about 55,000 tokens. Reflect sent every reasoning_content back, and the server's chat
template rendered all of it. Results of earlier steps are in the narration, the tool calls, and their results.
"""

import json

# Assistant messages, latest first, whose thinking goes back to the server. The latest thinking explains the
# pending tool calls; earlier steps show their conclusions in their narration and results.
KEPT_THINKING = 1
# Messages at the end of the conversation that shortening leaves as they are.
KEPT_RECENT = 12
# Strings in tool call arguments, and tool output, longer than this many characters are cut when shortened.
_LONG_ARGUMENT = 600
_LONG_OUTPUT = 1500
_HEAD = 400
_TAIL = 300


def without_old_thinking(messages, kept=KEPT_THINKING):
    """Leave out reasoning_content of all assistant messages but the latest `kept`."""
    result, seen = [], 0
    for message in reversed(messages):
        if message.get("role") == "assistant" and "reasoning_content" in message:
            seen += 1
            if seen > kept:
                message = {key: value for key, value in message.items() if key != "reasoning_content"}
        result.append(message)
    return list(reversed(result))


def _cut(text, head=_HEAD, tail=_TAIL):
    left = len(text) - head - tail
    return f"{text[:head]}\n[{left:,} characters left out to save context]\n{text[-tail:]}"


def _short_arguments(arguments):
    try:
        values = json.loads(arguments)
    except (TypeError, ValueError):
        return arguments
    if not isinstance(values, dict):
        return arguments
    changed = False
    for key, value in values.items():
        if isinstance(value, str) and len(value) > _LONG_ARGUMENT:
            values[key] = (f"{value[:_HEAD]}\n[{len(value) - _HEAD:,} more characters left out to save context. "
                           f"Read the file for its current text.]")
            changed = True
    return json.dumps(values, ensure_ascii=False) if changed else arguments


def _short_result(content):
    try:
        result = json.loads(content)
    except (TypeError, ValueError):
        return _cut(content) if isinstance(content, str) and len(content) > _LONG_OUTPUT else content
    if not isinstance(result, dict):
        return content
    changed = False
    if "image" in result:
        result.pop("image")
        result["output"] = (f"{result.get('output', '')}\n[The image is left out to save context. Use view_image "
                            f"to look at it again.]").strip()
        changed = True
    output = result.get("output")
    if isinstance(output, str) and len(output) > _LONG_OUTPUT:
        result["output"] = _cut(output)
        changed = True
    return json.dumps(result, ensure_ascii=False) if changed else content


def shorten(messages, count):
    """Shorten the messages before index `count`: cut long tool output and long tool call arguments, such as a
    file's content in write_file, and leave out images. The system prompt and user messages stay as they are.
    """
    shortened = []
    for index, message in enumerate(messages):
        if index < count:
            if message.get("role") == "tool":
                message = {**message, "content": _short_result(message.get("content"))}
            elif message.get("role") == "assistant" and message.get("tool_calls"):
                message = {**message, "tool_calls": [
                    {**call, "function": {**call["function"],
                                          "arguments": _short_arguments(call["function"].get("arguments"))}}
                    if isinstance(call.get("function"), dict) else call
                    for call in message["tool_calls"]]}
        shortened.append(message)
    return shortened


def shorten_point(length, kept=KEPT_RECENT):
    """The index before which a conversation of `length` messages is shortened, keeping the latest `kept`."""
    return max(1, length - kept)
