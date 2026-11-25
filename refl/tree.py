import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4


def ensure_git_ignored(directory):
    """Create the directory with a .gitignore that keeps Git from tracking its contents."""
    directory.mkdir(parents=True, exist_ok=True)
    ignore = directory / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")


@dataclass
class Node:
    id: int
    parent_id: Optional[int]
    branch_type: str
    message: dict
    status: str = "active"


class ConversationTree:
    def __init__(self, directory, system_prompt, resume=None, *, persist=True):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.path = directory / f"{timestamp}-{uuid4().hex}.json"
        self.nodes = []
        self.head = None
        self.reflection = {"delay": 0, "remaining": 0}
        if resume is None:
            self.append({"role": "system", "content": system_prompt})
        else:
            self._load(directory, resume)
        if persist:
            self.save()

    def _load(self, directory, session_id):
        if not re.fullmatch(r"\d{8}T\d{12}Z-[0-9a-f]{32}", session_id):
            raise ValueError("Expected a session ID printed by refl")
        path = directory / f"{session_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["version"] != 1:
                raise ValueError("Unsupported session version")
            nodes = [Node(**entry) for entry in data["nodes"]]
            head = data["main_head"]
            pacing = data.get("reflection", self.reflection)
            delay, remaining = pacing["delay"], pacing["remaining"]
            if any(type(value) is not int for value in (delay, remaining)) or not 0 <= remaining <= delay:
                raise ValueError("Invalid reflection pacing")
            if type(head) is not int or not 0 <= head < len(nodes):
                raise ValueError("Invalid conversation head")
            for index, node in enumerate(nodes):
                parent = node.parent_id
                if (type(node.id) is not int or node.id != index or not isinstance(node.message, dict)
                        or node.message.get("role") not in {"system", "user", "assistant", "tool"}
                        or node.branch_type not in {"main", "verification", "reflection", "challenge", "moderator"}
                        or node.status not in {"active", "merged", "pruned"}):
                    raise ValueError("Invalid conversation node")
                if index == 0:
                    if parent is not None or node.message.get("role") != "system":
                        raise ValueError("Invalid root node")
                elif type(parent) is not int or not 0 <= parent < index:
                    raise ValueError("Invalid parent link")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid session {session_id}: {error}") from error
        self.nodes, self.head = nodes, head
        self.reflection.update(delay=delay, remaining=remaining)

    def append(self, message, *, parent_id=None, branch_type="main"):
        if branch_type == "main":
            parent_id = self.head
        node = Node(len(self.nodes), parent_id, branch_type, message)
        self.nodes.append(node)
        if branch_type == "main":
            self.head = node.id
        return node.id

    def messages(self, leaf, *, start=None):
        """Return the messages from the root, or from the `start` node, down to `leaf`."""
        path = []
        while leaf is not None:
            node = self.nodes[leaf]
            path.append(node.message)
            if leaf == start:
                break
            leaf = node.parent_id
        return list(reversed(path))

    def checkout(self, node_id):
        if type(node_id) is not int or not 0 <= node_id < len(self.nodes):
            raise ValueError(f"No conversation node {node_id}. Use /tree to see the available IDs.")
        self.head = node_id
        self.save()

    def render(self):
        children = {node.id: [] for node in self.nodes}
        for node in self.nodes[1:]:
            children[node.parent_id].append(node.id)

        # The newest child continues at the same indentation, so a long conversation stays flat.
        # Earlier children are forks or abandoned branches and are drawn as indented branches first.
        lines = []
        pending = [(0, "", "")]
        while pending:
            node_id, prefix, child_prefix = pending.pop()
            node = self.nodes[node_id]
            role = node.message.get("role", "")
            content = node.message.get("content") or ""
            if isinstance(content, str):
                preview = " ".join("".join(c if c.isprintable() else " " for c in content).split())
            else:
                preview = ""
            if not preview and node.message.get("tool_calls"):
                preview = "Tool calls"
            if len(preview) > 68:
                preview = preview[:65] + "..."
            head = " [current]" if node_id == self.head else ""
            lines.append(f"{prefix}{node_id} {node.branch_type}/{role} ({node.status}){head} {preview}".rstrip())
            kids = children[node_id]
            if kids:
                pending.append((kids[-1], child_prefix, child_prefix))
                for kid in reversed(kids[:-1]):
                    pending.append((kid, child_prefix + "+-- ", child_prefix + "|   "))
        return "\n".join(lines)

    def finish_fork(self, leaf, anchor, status):
        while leaf != anchor:
            node = self.nodes[leaf]
            node.status = status
            leaf = node.parent_id

    def save(self):
        ensure_git_ignored(self.path.parent)
        data = {
            "version": 1,
            "main_head": self.head,
            "reflection": self.reflection,
            "nodes": [asdict(node) for node in self.nodes],
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=self.path.parent, delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
