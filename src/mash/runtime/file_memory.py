"""File memory on the agent's computer.

Memory is markdown under the memory directory (``/home/agent/memory`` on a
computer): ``INDEX.md`` lists one file per line, and each file holds one
topic. Every request's system prompt carries ``INDEX.md``; the agent reads full
files with ``cat``.

The agent does not write memory while it works. After a request completes, the
runtime enqueues a memory update: an ordinary request of the same agent, run
on a queue local to the computer with concurrency 1, so it is the only writer.
It gets a curator prompt, the completed request's transcript, the current
index, and bash in the memory directory, and nothing else.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MASH_MEMORY_DIR_ENV = "MASH_MEMORY_DIR"
DEFAULT_MEMORY_DIR = "/home/agent/memory"
INDEX_FILE = "INDEX.md"
MEMORY_UPDATE_KEY = "memory_update"
TRACE_KIND = "memory.update"

_TRANSCRIPT_MAX_CHARS = 30_000
_TRANSCRIPT_PART_MAX_CHARS = 4_000


def resolve_memory_dir(config: Any) -> Path | None:
    """The memory directory for an agent, or None when memory is off."""
    if not getattr(config, "memory_enabled", False):
        return None
    path = Path(os.environ.get(MASH_MEMORY_DIR_ENV) or DEFAULT_MEMORY_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_index(memory_dir: Path, max_chars: int) -> str:
    """``INDEX.md`` capped at ``max_chars``; empty when there is none."""
    try:
        text = (memory_dir / INDEX_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    if len(text) > max_chars:
        text = f"{text[:max_chars].rstrip()}\n[INDEX.md truncated at {max_chars} characters]"
    return text


def memory_block(memory_dir: Path, index_text: str) -> str:
    """The memory section of a request's system prompt."""
    if not index_text:
        return ""
    return (
        "## Memory\n\n"
        f"Your memory is in {memory_dir}. {INDEX_FILE} lists what each file holds:\n\n"
        f"{index_text}\n\n"
        "Read a file with `cat` when it bears on the request."
    )


def memory_update_prompt(agent_id: str, memory_dir: Path, max_chars: int) -> str:
    return (
        f"You maintain the long-term memory of the agent '{agent_id}'.\n\n"
        f"Memory is markdown files in {memory_dir}. {INDEX_FILE} has one line per "
        "file: `- [name.md](name.md): what it holds`. Each file holds one topic: "
        "facts about users and systems, procedures and lessons learned, ongoing "
        "work, or pointers into the workspace.\n\n"
        "The user message is the transcript of one request the agent just "
        "completed. Record what will help in future requests. Skip what is "
        "already recorded, details that only mattered for this request, and the "
        "conversation itself, which is stored separately.\n\n"
        "Use bash to edit or add files, and update "
        f"{INDEX_FILE} in the same pass so it matches the files. Keep "
        f"{INDEX_FILE} under {max_chars} characters: when it nears that, merge "
        "related files and delete ones that no longer hold. If nothing is worth "
        "keeping, change nothing.\n\n"
        "Finish with one line saying what you changed."
    )


def should_update_memory(request_metadata: dict[str, Any]) -> bool:
    """Top-level and subagent requests update memory; workflow steps and
    memory updates themselves do not."""
    metadata = request_metadata or {}
    return not metadata.get(MEMORY_UPDATE_KEY) and not metadata.get("workflow_id")


def memory_update_request_id(request_id: str) -> str:
    return f"{request_id}-memory"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}... [{len(text) - limit} more characters]"


def _render_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=True, default=str)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "tool_use":
            arguments = json.dumps(block.get("input") or {}, ensure_ascii=True, default=str)
            parts.append(f"[tool call {block.get('name')}: {arguments}]")
        elif kind == "tool_result":
            prefix = "tool error" if block.get("is_error") else "tool result"
            parts.append(f"[{prefix}: {_render_content(block.get('content'))}]")
        elif kind in ("thinking", "redacted_thinking"):
            continue
        else:
            parts.append(json.dumps(block, ensure_ascii=True, default=str))
    return "\n".join(part for part in parts if part)


def build_transcript(message: str, context_payload: dict[str, Any]) -> str:
    """The completed request as text: its user message onward, capped."""
    messages = list((context_payload or {}).get("messages") or [])
    start = 0
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        if item.get("role") == "user" and _render_content(item.get("content")) == message:
            start = index
            break
    lines: list[str] = []
    for item in messages[start:]:
        text = _clip(_render_content(item.get("content")).strip(), _TRANSCRIPT_PART_MAX_CHARS)
        if text:
            lines.append(f"{item.get('role')}: {text}")
    if not lines:
        lines.append(f"user: {message}")
    return _clip("\n\n".join(lines), _TRANSCRIPT_MAX_CHARS)


__all__ = [
    "DEFAULT_MEMORY_DIR",
    "INDEX_FILE",
    "MASH_MEMORY_DIR_ENV",
    "MEMORY_UPDATE_KEY",
    "TRACE_KIND",
    "build_transcript",
    "memory_block",
    "memory_update_prompt",
    "memory_update_request_id",
    "read_index",
    "resolve_memory_dir",
    "should_update_memory",
]
