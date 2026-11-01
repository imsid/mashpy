"""Read what the projection needs from the runtime store."""

from __future__ import annotations

from typing import Any

from ..runtime.events.types import RuntimeEvent, RuntimeEventType
from .projection import MAX_CHILD_DEPTH


async def load_request_tree(
    store: Any, request_id: str
) -> tuple[list[RuntimeEvent], dict[str, list[RuntimeEvent]]]:
    """A request's events and, by request id, its RunSubAgents children's.

    Children come from the ids each ``execute_tool.start`` of RunSubAgents
    records, so they nest under the call that started them.
    """
    events = await store.list_request_events(request_id)
    children: dict[str, list[RuntimeEvent]] = {}
    pending = [(events, 0)]
    while pending:
        current, depth = pending.pop()
        if depth >= MAX_CHILD_DEPTH:
            continue
        for child_id in _child_request_ids(current):
            if child_id in children:
                continue
            child_events = await store.list_request_events(child_id)
            if child_events:
                children[child_id] = child_events
                pending.append((child_events, depth + 1))
    return events, children


def _child_request_ids(events: list[RuntimeEvent]) -> list[str]:
    ids: list[str] = []
    for event in events:
        if event.event_type != RuntimeEventType.EXECUTE_TOOL_START.value:
            continue
        for item in (event.payload or {}).get("subagents") or []:
            if isinstance(item, dict) and item.get("request_id"):
                ids.append(str(item["request_id"]))
    return ids
