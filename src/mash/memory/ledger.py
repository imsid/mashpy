"""The ledger: what an agent's RunSubAgents and RunWorkflow calls returned.

A turn's ``metadata["ledger"]`` holds one entry per ``RunSubAgents`` item and
per ``RunWorkflow`` call the request made. Replay appends the entries to the
turn's assistant message, so the agent sees which session or run produced
each reply when it routes the next message.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

ASKED_CHARS = 200
GOT_CHARS = 500
# Entries a summary checkpoint keeps: the latest per agent session or run.
MAX_CHECKPOINT_ENTRIES = 30


def _clip(text: Any, limit: int) -> str:
    value = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _tool_calls(messages: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    calls: Dict[str, Dict[str, Any]] = {}
    for message in messages:
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_call":
                calls[str(block.get("id") or "")] = block
    return calls


def _subagent_entries(
    call: Dict[str, Any], content: str, session_id: str
) -> List[Dict[str, Any]]:
    try:
        results = json.loads(content)
    except (TypeError, ValueError):
        return []
    if not isinstance(results, list):
        return []
    items = (call.get("arguments") or {}).get("requests")
    items = items if isinstance(items, list) else []
    entries: List[Dict[str, Any]] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            continue
        item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
        if result.get("structured_output") is not None:
            got = result["structured_output"]
        else:
            got = result.get("response") or result.get("error") or ""
        entries.append(
            {
                "companion_session_id": session_id,
                "agent_id": result.get("agent_id"),
                "session_id": result.get("session_id"),
                "trace_id": result.get("trace_id"),
                "status": result.get("status"),
                "asked": _clip(item.get("prompt") or "", ASKED_CHARS),
                "got": _clip(got, GOT_CHARS),
            }
        )
    return entries


def _workflow_entry(
    call: Dict[str, Any], content: str, session_id: str
) -> List[Dict[str, Any]]:
    try:
        result = json.loads(content)
    except (TypeError, ValueError):
        return []
    if not isinstance(result, dict) or not result.get("run_id"):
        return []
    arguments = call.get("arguments") or {}
    got = result.get("result") if result.get("result") is not None else result.get("error")
    return [
        {
            "companion_session_id": session_id,
            "workflow_id": arguments.get("workflow_id"),
            "run_id": result.get("run_id"),
            "trace_id": result.get("trace_id"),
            "status": result.get("status"),
            "asked": _clip(arguments.get("input") or {}, ASKED_CHARS),
            "got": _clip(got if got is not None else "", GOT_CHARS),
        }
    ]


def build_ledger(context_payload: Dict[str, Any], session_id: str) -> List[Dict[str, Any]]:
    """Ledger entries from the request's ``RunSubAgents`` and ``RunWorkflow`` results.

    ``context_payload`` is the request's serialized context; replayed history
    messages carry plain text and no tool calls, so only this request's calls
    count.
    """
    messages = [m for m in context_payload.get("messages") or [] if isinstance(m, dict)]
    calls = _tool_calls(messages)
    entries: List[Dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") != "tool" or not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if block.get("is_error"):
                continue
            call = calls.get(str(block.get("tool_call_id") or ""))
            if call is None:
                continue
            name = call.get("name")
            text = str(block.get("content") or "")
            if name == "RunSubAgents":
                entries.extend(_subagent_entries(call, text, session_id))
            elif name == "RunWorkflow":
                entries.extend(_workflow_entry(call, text, session_id))
    return entries


def _key(entry: Dict[str, Any]) -> tuple:
    if entry.get("workflow_id"):
        return ("workflow", entry.get("workflow_id"), entry.get("run_id"))
    return ("agent", entry.get("agent_id"), entry.get("session_id"))


def merge_ledgers(
    ledgers: Iterable[Iterable[Dict[str, Any]]], *, limit: int = MAX_CHECKPOINT_ENTRIES
) -> List[Dict[str, Any]]:
    """The latest entry per agent session or workflow run, oldest first, at most ``limit``."""
    latest: Dict[tuple, Dict[str, Any]] = {}
    for ledger in ledgers:
        for entry in ledger or []:
            if isinstance(entry, dict):
                key = _key(entry)
                latest.pop(key, None)
                latest[key] = entry
    return list(latest.values())[-limit:] if limit > 0 else []


def format_ledger(entries: Iterable[Dict[str, Any]]) -> str:
    """The ``[ledger]`` block replay appends to a turn's assistant message."""
    lines = ["[ledger]"]
    for entry in entries:
        if entry.get("workflow_id"):
            head = f"- workflow {entry.get('workflow_id')}  run {entry.get('run_id')}"
        else:
            head = f"- {entry.get('agent_id')}  session {entry.get('session_id')}"
        if entry.get("trace_id"):
            head += f"  trace {entry.get('trace_id')}"
        head += f"  {entry.get('status')}"
        lines.append(head)
        lines.append(f"  asked: {json.dumps(entry.get('asked') or '', ensure_ascii=False)}")
        lines.append(f"  got: {json.dumps(entry.get('got') or '', ensure_ascii=False)}")
    return "\n".join(lines) if len(lines) > 1 else ""


__all__ = ["build_ledger", "format_ledger", "merge_ledgers"]
