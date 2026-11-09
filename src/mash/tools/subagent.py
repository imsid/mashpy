"""RunSubAgents: run child requests at the same time and wait for all of them."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
import uuid
from contextlib import contextmanager
from typing import Any, AsyncIterator, Callable, Dict, Iterator, Optional, Protocol

from ..logging import (
    get_request_id,
    get_request_metadata,
    get_subagent_id_prefix,
)
from ..runtime.client import InteractionClosedError
from ..runtime.errors import classify_error, retry_transient
from .base import ToolResult

logger = logging.getLogger(__name__)

# The default and the ceiling of a call's timeout_seconds; an agent sets its
# own with AgentConfig.subagent_timeout_seconds.
DEFAULT_TIMEOUT_SECONDS = 3600
# Children one call may start: each is a request on its agent's process.
MAX_REQUESTS = 16

# Child ids are UUID5s in this namespace, so the REPL's 8-character short ids
# stay distinct; a ``{request_id}-sub-...`` string would share the parent's
# prefix.
CHILD_REQUEST_NAMESPACE = uuid.UUID("6f0d3b1e-8c47-4f53-9a8e-2b5d1c7e4a90")


def child_request_id(prefix: str, index: int) -> str:
    """The id of the ``index``-th child of the call bound to ``prefix``."""
    return str(uuid.uuid5(CHILD_REQUEST_NAMESPACE, f"{prefix}-{index}"))


class SupportsChildClient(Protocol):
    """The H2A client operations RunSubAgents uses for one child."""

    async def post_request(
        self,
        message: str,
        *,
        session_id: str,
        structured_output: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
        host: Optional[Dict[str, Any]] = None,
        parent: Optional[Dict[str, Any]] = None,
        mode: Optional[str] = None,
    ) -> str:
        """Submit one request; a known ``request_id`` returns the existing one."""

    def stream_response(
        self,
        request_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yield the child's streamed events."""

    async def cancel_request(self, request_id: str) -> Dict[str, Any]:
        """Cancel the child."""

    async def post_interaction(
        self, request_id: str, *, interaction_id: str, response: Any
    ) -> Dict[str, Any]:
        """Answer one of the child's interactions."""


class InteractionRelay(Protocol):
    """The calling request's side of its children's interactions.

    The request loop binds one around a RunSubAgents call. It shows each
    interaction a child opens to the user as an interaction of the calling
    request, under an id derived from the child's request and interaction ids.
    """

    async def open(self, interaction: Dict[str, Any]) -> str:
        """Open the relayed copy of a child's ``request.interaction.create``; return its id."""

    async def wait(self, interaction_id: str) -> Dict[str, Any]:
        """Wait for the relayed interaction to close; return its ack payload."""

    async def close(self, interaction_id: str, ack: Dict[str, Any]) -> None:
        """Close the relayed interaction as the child's ack closed the child's."""


_interaction_relay: contextvars.ContextVar[Optional[InteractionRelay]] = (
    contextvars.ContextVar("mash_interaction_relay", default=None)
)


def get_interaction_relay() -> Optional[InteractionRelay]:
    """The relay bound for the current RunSubAgents call, if any."""
    return _interaction_relay.get()


@contextmanager
def bound_interaction_relay(relay: Optional[InteractionRelay]) -> Iterator[None]:
    """Bind the relay for one RunSubAgents call."""
    token = _interaction_relay.set(relay)
    try:
        yield
    finally:
        _interaction_relay.reset(token)


class _Budget:
    """One child's share of timeout_seconds; its clock stops while it waits on the user."""

    def __init__(self, seconds: float) -> None:
        self._left = float(seconds)
        self._since = time.monotonic()
        self._waiting = 0
        self.changed = asyncio.Event()

    def pause(self) -> None:
        if self._waiting == 0:
            self._left -= time.monotonic() - self._since
        self._waiting += 1
        self.changed.set()

    def resume(self) -> None:
        self._waiting -= 1
        if self._waiting == 0:
            self._since = time.monotonic()
        self.changed.set()

    def left(self) -> Optional[float]:
        """Seconds left, or None while the clock is stopped."""
        if self._waiting:
            return None
        return self._left - (time.monotonic() - self._since)


def _interaction_item(opened: Dict[str, Any], ack: Dict[str, Any]) -> Dict[str, Any]:
    """A result entry's record of one relayed interaction."""
    item = {
        "interaction_id": opened.get("interaction_id"),
        "type": opened.get("type"),
        "prompt": opened.get("prompt"),
        "response": ack.get("response"),
    }
    for flag in ("timed_out", "cancelled"):
        if ack.get(flag):
            item[flag] = True
    return item


def _child_error(data: Dict[str, Any]) -> Dict[str, Any]:
    error = classify_error(data.get("error") or "request failed")
    for key in ("error_code", "retryable"):
        if data.get(key) is not None:
            error[key] = data.get(key)
    return error


class RunSubAgentsTool:
    """Start one or more ``bg`` child requests and return when all are done."""

    name = "RunSubAgents"
    requires_approval = False
    # The concurrency is inside the one call.
    parallel_safe = False
    description = (
        "Run one or more requests at the same time, each on one of the host's "
        "agents, and wait for all of them. Use one item to hand a task to an "
        "agent; use several for parts that do not depend on each other, then "
        "write one answer from their results. Returns one entry per item, in "
        "order, with the child's request_id, its status (completed, failed, "
        "cancelled or timed_out), and its response, structured output or "
        "error, plus the session_id it ran in and its trace_id. When a child "
        "asked the user something (an approval, a question, a sign-in), its "
        "entry lists those interactions with the user's response. Each item runs "
        "in a session of its own: pass an earlier entry's session_id to "
        "continue that session, where the agent sees its own earlier work, or "
        "leave it out to start a new one."
    )
    parameters = {
        "type": "object",
        "properties": {
            "requests": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_REQUESTS,
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "One of the host's agents.",
                        },
                        "prompt": {"type": "string"},
                        "session_id": {
                            "type": "string",
                            "description": (
                                "Optional: an earlier session of this agent to "
                                "continue. Without it the child starts a new session."
                            ),
                        },
                        "output_schema": {
                            "type": "object",
                            "description": "Optional JSON schema for the child's structured output.",
                        },
                    },
                    "required": ["agent_id", "prompt"],
                },
            },
        },
        "required": ["requests"],
    }

    def __init__(
        self,
        *,
        caller_agent_id: str,
        client_resolver: Callable[[str], SupportsChildClient],
        session_id: str,
        host: Optional[Dict[str, Any]] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if client_resolver is None:
            raise ValueError("client_resolver is required")
        if not str(session_id or "").strip():
            raise ValueError("session_id is required")
        self._caller_agent_id = caller_agent_id
        self._client_resolver = client_resolver
        self._session_id = str(session_id).strip()
        self._host = dict(host) if host is not None else None
        if isinstance(timeout_seconds, bool) or int(timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = int(timeout_seconds)
        properties = dict(type(self).parameters["properties"])
        properties["timeout_seconds"] = {
            "type": "integer",
            "description": (
                f"Defaults to {self._timeout_seconds}, at most {self._timeout_seconds}."
            ),
            "maximum": self._timeout_seconds,
        }
        self.parameters = {**type(self).parameters, "properties": properties}

    def _parse(self, args: Dict[str, Any]) -> tuple[list[Dict[str, Any]], int] | str:
        requests = args.get("requests")
        if not isinstance(requests, list) or not requests:
            return "requests must be a non-empty array"
        if len(requests) > MAX_REQUESTS:
            return f"requests may have at most {MAX_REQUESTS} items"
        items: list[Dict[str, Any]] = []
        for index, item in enumerate(requests):
            if not isinstance(item, dict):
                return f"requests[{index}] must be an object"
            agent_id = str(item.get("agent_id") or "").strip()
            prompt = str(item.get("prompt") or "").strip()
            output_schema = item.get("output_schema")
            if not agent_id:
                return f"requests[{index}].agent_id is required"
            if not prompt:
                return f"requests[{index}].prompt is required"
            if output_schema is not None and not isinstance(output_schema, dict):
                return f"requests[{index}].output_schema must be an object"
            session_id = item.get("session_id")
            if session_id is not None and (
                not isinstance(session_id, str) or not session_id.strip()
            ):
                return f"requests[{index}].session_id must be a non-empty string"
            items.append(
                {
                    "agent_id": agent_id,
                    "prompt": prompt,
                    "output_schema": output_schema,
                    "session_id": session_id.strip() if session_id else None,
                }
            )
        timeout = args.get("timeout_seconds", self._timeout_seconds)
        if timeout is None:
            timeout = self._timeout_seconds
        if isinstance(timeout, bool):
            return "timeout_seconds must be a positive integer"
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            return "timeout_seconds must be a positive integer"
        if not 0 < timeout <= self._timeout_seconds:
            return f"timeout_seconds must be a positive integer, at most {self._timeout_seconds}"
        return items, timeout

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        parsed = self._parse(args)
        if isinstance(parsed, str):
            return ToolResult.error(parsed)
        items, timeout_seconds = parsed

        started_at = time.monotonic()
        caller_request_id = get_request_id() or ""
        # The parent request's caller metadata is bound for its run; forward it
        # so the children inherit the context.
        inherited_metadata = get_request_metadata()
        parent = {
            "kind": "subagent",
            "request_id": caller_request_id,
            "agent_id": self._caller_agent_id,
            "session_id": self._session_id,
            "started_by": self._caller_agent_id,
            "parent_request_id": caller_request_id,
        }
        # The durable loop binds a prefix fixed by this call's position, so a
        # replayed or retried call resubmits the same ids and gets back the
        # children already running instead of starting new ones.
        prefix = get_subagent_id_prefix()

        entries: list[Dict[str, Any]] = [{} for _ in items]
        submitted: list[tuple[int, Any, str]] = []
        for index, item in enumerate(items):
            agent_id = item["agent_id"]
            # A new child session takes the child's request id, so a replayed
            # call lands in the same session.
            child_id = child_request_id(prefix, index) if prefix else str(uuid.uuid4())
            session_id = item["session_id"] or child_id
            entry: Dict[str, Any] = {
                "agent_id": agent_id,
                "request_id": None,
                "session_id": session_id,
            }
            entries[index] = entry
            try:
                client = self._client_resolver(agent_id)
                request_id = await client.post_request(
                    item["prompt"],
                    session_id=session_id,
                    structured_output=item["output_schema"],
                    metadata=inherited_metadata or None,
                    request_id=child_id,
                    host=self._host,
                    parent=parent,
                    mode="bg",
                )
            except Exception as exc:
                entry.update({"status": "failed", **classify_error(exc)})
                continue
            entry["request_id"] = request_id
            submitted.append((index, client, request_id))

        await asyncio.gather(
            *(
                self._wait(entries[index], client, request_id, timeout_seconds)
                for index, client, request_id in submitted
            )
        )

        for entry in entries:
            entry.setdefault("status", "failed")
        agent_ids = list(dict.fromkeys(item["agent_id"] for item in items))
        request_ids = [entry.get("request_id") for entry in entries]
        return ToolResult.success(
            json.dumps(entries, ensure_ascii=True),
            agent_id=", ".join(agent_ids),
            agent_ids=agent_ids,
            primary_session_id=self._session_id,
            subagent_session_ids=[entry.get("session_id") for entry in entries],
            request_id=request_ids[0] if len(request_ids) == 1 else None,
            request_ids=request_ids,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )

    async def _wait(
        self,
        entry: Dict[str, Any],
        client: Any,
        request_id: str,
        timeout_seconds: int,
    ) -> None:
        """Follow one child's stream into ``entry`` until it ends or its budget runs out.

        The budget's clock stops while the child waits on the user.
        """
        budget = _Budget(timeout_seconds)
        follow = asyncio.ensure_future(self._follow(entry, client, request_id, budget))
        try:
            while not follow.done():
                left = budget.left()
                if left is not None and left <= 0:
                    follow.cancel()
                    await asyncio.gather(follow, return_exceptions=True)
                    entry.update(
                        {
                            "status": "timed_out",
                            "error": "the child did not finish before timeout_seconds",
                        }
                    )
                    try:
                        await client.cancel_request(request_id)
                    except Exception:
                        pass
                    return
                budget.changed.clear()
                changed = asyncio.ensure_future(budget.changed.wait())
                try:
                    await asyncio.wait(
                        {follow, changed}, timeout=left, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    changed.cancel()
            follow.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            entry.update({"status": "failed", **classify_error(exc)})
        finally:
            if not follow.done():
                follow.cancel()
        if "status" not in entry:
            entry.update(
                {
                    "status": "failed",
                    "error": "the child's stream ended without a terminal event",
                }
            )

    async def _follow(
        self, entry: Dict[str, Any], client: Any, request_id: str, budget: _Budget
    ) -> None:
        """Read the child's stream into ``entry``, relaying its interactions.

        The stream replays from the start, so a rerun call opens and closes the
        relayed interactions again; both are no-ops on ids already stored.
        """
        relay = get_interaction_relay()
        opened: Dict[str, Dict[str, Any]] = {}  # child interaction id -> its create
        relayed: Dict[str, str] = {}  # child interaction id -> relayed id
        forwards: list[asyncio.Task[None]] = []
        interactions: list[Dict[str, Any]] = []
        stopped = False
        try:
            async for event in client.stream_response(request_id):
                event_name = str(event.get("event") or "")
                data = event.get("data")
                if not isinstance(data, dict):
                    data = {}
                if event_name == "request.interaction.create" and relay is not None:
                    child_interaction_id = str(data.get("interaction_id") or "")
                    relayed_id = await relay.open(data)
                    opened[child_interaction_id] = data
                    relayed[child_interaction_id] = relayed_id
                    budget.pause()
                    forwards.append(
                        asyncio.ensure_future(
                            self._forward(
                                relay, client, request_id, child_interaction_id, relayed_id
                            )
                        )
                    )
                    continue
                if event_name == "request.interaction.ack":
                    child_interaction_id = str(data.get("interaction_id") or "")
                    relayed_id = relayed.pop(child_interaction_id, None)
                    if relayed_id is None:
                        continue
                    await relay.close(relayed_id, data)
                    budget.resume()
                    interactions.append(
                        _interaction_item(opened.pop(child_interaction_id), data)
                    )
                    continue
                if event_name == "request.completed":
                    response = data.get("response")
                    response = response if isinstance(response, dict) else {}
                    metadata = response.get("metadata")
                    metadata = metadata if isinstance(metadata, dict) else {}
                    entry["status"] = "completed"
                    if metadata.get("trace_id"):
                        entry["trace_id"] = str(metadata["trace_id"])
                    entry["response"] = str(response.get("text") or "")
                    if metadata.get("structured_output") is not None:
                        entry["structured_output"] = metadata["structured_output"]
                    return
                if event_name == "request.cancelled":
                    entry["status"] = "cancelled"
                    return
                if event_name == "request.error":
                    entry.update({"status": "failed", **_child_error(data)})
                    return
        except asyncio.CancelledError:
            # The call stops here (its budget ran out, or the process is
            # going down); a rerun call picks the open interactions up again.
            stopped = True
            raise
        finally:
            for task in forwards:
                task.cancel()
            await asyncio.gather(*forwards, return_exceptions=True)
            if not stopped:
                # The child ended or its stream broke: nothing answers these now.
                for relayed_id in relayed.values():
                    await relay.close(relayed_id, {"response": None, "cancelled": True})
            if interactions:
                entry["interactions"] = interactions

    async def _forward(
        self,
        relay: InteractionRelay,
        client: Any,
        request_id: str,
        child_interaction_id: str,
        relayed_id: str,
    ) -> None:
        """Send the user's answer to the relayed interaction on to the child."""
        ack = await relay.wait(relayed_id)
        if ack.get("cancelled") or ack.get("timed_out"):
            return
        try:
            await retry_transient(
                lambda: client.post_interaction(
                    request_id,
                    interaction_id=child_interaction_id,
                    response=ack.get("response"),
                )
            )
        except InteractionClosedError:
            # The child already has an answer: its own timeout, or this one
            # forwarded before a restart.
            pass
        except Exception:
            logger.warning(
                "could not answer interaction %s of child request %s",
                child_interaction_id,
                request_id,
                exc_info=True,
            )

    def to_llm_format(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }
