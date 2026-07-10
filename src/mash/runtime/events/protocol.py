"""RuntimeStore protocol — the append-only event store interface."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from .types import FeedbackRecord, RuntimeEvent


class RuntimeStore(Protocol):
    """Append-only runtime event store."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def append_event(self, event: RuntimeEvent) -> RuntimeEvent: ...

    async def list_request_events(
        self,
        request_id: str,
        *,
        after_seq: int = 0,
    ) -> list[RuntimeEvent]: ...

    async def list_session_events(
        self,
        session_id: str,
        *,
        event_types: list[str] | None = None,
    ) -> list[RuntimeEvent]: ...

    async def list_events(
        self,
        app_id: str,
        *,
        session_id: str | None = None,
        trace_id: str | None = None,
        host_id: str | None = None,
        workflow_run_id: str | None = None,
        event_type_prefix: str | None = None,
        after_event_id: int = 0,
        limit: int | None = None,
    ) -> list[RuntimeEvent]: ...

    async def has_request(self, request_id: str) -> bool: ...

    async def is_request_terminal(self, request_id: str) -> bool: ...

    async def append_feedback(self, feedback: FeedbackRecord) -> FeedbackRecord: ...

    async def list_feedback(
        self,
        app_id: str,
        *,
        after: float,
        before: float | None = None,
        feedback_type: str | None = None,
        session_id: str | None = None,
        q: str | None = None,
        limit: int | None = None,
    ) -> list[FeedbackRecord]: ...

    async def get_latest_trace(
        self,
        app_id: str,
        session_id: str,
    ) -> dict[str, Any] | None: ...

    async def list_recent_traces(
        self,
        app_id: str | None = None,
        *,
        session_id: str | None = None,
        host_id: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]: ...

    async def list_sessions(
        self,
        *,
        agent_id: str | None = None,
        workflow_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]: ...

    async def aggregate_workflow_activity(self) -> list[dict[str, Any]]: ...

    async def aggregate_usage(
        self,
        app_id: str,
        *,
        host_id: str | None = None,
        session_id: str | None = None,
        bucket: str = "day",
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> list[dict[str, Any]]: ...

    async def count_tool_invocations(
        self,
        app_id: str,
        *,
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> list[dict[str, Any]]: ...

    async def count_skill_invocations(
        self,
        app_id: str,
        *,
        from_ts: float | None = None,
        to_ts: float | None = None,
    ) -> list[dict[str, Any]]: ...

    def register_request_waiter(self, request_id: str) -> asyncio.Event: ...

    def unregister_request_waiter(
        self, request_id: str, event: asyncio.Event
    ) -> None: ...

    def register_global_waiter(self) -> asyncio.Event: ...

    def unregister_global_waiter(self, event: asyncio.Event) -> None: ...
