"""Routes: the agent sessions and workflow runs that handled a turn.

A turn whose request called ``RunSubAgents`` or ``RunWorkflow`` stores
``metadata["routes"]``: one entry per child request and per workflow run, with
its ids and status, read from the calls' full results when the turn is saved.
An agent that replays replies (the companion) sees them before the next user
message, so it knows which session or run produced each reply and can continue
it. What the agents were asked and answered stays in the event log, where the
companion's log tools read it.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Tuple


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def build_routes(calls: Iterable[Tuple[str, Dict[str, Any], str]]) -> List[Dict[str, Any]]:
    """Route entries from a request's successful calls: ``(tool_name, arguments, result)``.

    ``result`` is the call's full result text, as ``execute_tool.end`` stores it.
    """
    routes: List[Dict[str, Any]] = []
    for name, arguments, content in calls:
        if name == "RunSubAgents":
            entries = _json(content)
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                routes.append(
                    {
                        "agent_id": entry.get("agent_id"),
                        "session_id": entry.get("session_id"),
                        "trace_id": entry.get("trace_id"),
                        "status": entry.get("status"),
                    }
                )
        elif name == "RunWorkflow":
            result = _json(content)
            if not isinstance(result, dict) or not result.get("run_id"):
                continue
            routes.append(
                {
                    "workflow_id": (arguments or {}).get("workflow_id"),
                    "run_id": result.get("run_id"),
                    "trace_id": result.get("trace_id"),
                    "status": result.get("status"),
                }
            )
    return routes


def format_routes(routes: Iterable[Dict[str, Any]]) -> str:
    """The ``<routes>`` block replay puts before the user message after a reply."""
    lines = []
    for route in routes:
        if not isinstance(route, dict):
            continue
        if route.get("workflow_id"):
            line = f"- workflow {route.get('workflow_id')}  run {route.get('run_id')}"
        else:
            line = f"- {route.get('agent_id')}  session {route.get('session_id')}"
        if route.get("trace_id"):
            line += f"  trace {route.get('trace_id')}"
        lines.append(f"{line}  {route.get('status')}")
    if not lines:
        return ""
    return "\n".join(
        [
            "<routes>",
            "Agent sessions and workflow runs behind your previous reply, recorded by the system:",
            *lines,
            "</routes>",
        ]
    )


__all__ = ["build_routes", "format_routes"]
