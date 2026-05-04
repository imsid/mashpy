"""Workflow engine interfaces for runtime request execution."""

from __future__ import annotations

from typing import Any, Protocol


class RequestEngine(Protocol):
    async def open(self) -> None:
        ...

    async def close(self) -> None:
        ...

    async def start_request(
        self,
        *,
        request_id: str,
        message: str,
        session_id: str,
        request_metadata: dict[str, Any],
    ) -> None:
        ...
