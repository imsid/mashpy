"""Provider-neutral LLM interfaces and shared base implementation."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, List, Optional

from ...logging import EventLogger, LLMEvent
from ...logging.trace_context import get_loop_index
from .types import (
    LLMCapabilities,
    LLMRequest,
    LLMResponse,
)

log = logging.getLogger(__name__)


# Coalescing thresholds for streamed response deltas. Deltas are flushed into a
# single ``chat.delta`` event when the buffer reaches this many
# characters, or this many seconds elapse since the last flush (whichever comes
# first). This bounds event volume (~tens per turn instead of one per token
# chunk) while keeping the visible stream responsive.
DEFAULT_DELTA_MAX_CHARS = 80
DEFAULT_DELTA_MAX_INTERVAL = 0.5


class _DeltaStream:
    """Coalesces streamed text deltas into bounded ``chat.delta`` events.

    Providers push raw text chunks via :meth:`push`; the stream flushes a
    coalesced delta event when the buffer crosses the size or time threshold,
    and :meth:`flush` emits any trailing partial buffer when the stream ends.
    """

    def __init__(
        self,
        emit: Callable[[str, int], Awaitable[None]],
        *,
        max_chars: int = DEFAULT_DELTA_MAX_CHARS,
        max_interval: float = DEFAULT_DELTA_MAX_INTERVAL,
    ) -> None:
        self._emit = emit
        self._max_chars = max_chars
        self._max_interval = max_interval
        self._buffer = ""
        self._index = 0
        self._last_flush = time.monotonic()

    async def push(self, text: str) -> None:
        if not text:
            return
        self._buffer += text
        elapsed = time.monotonic() - self._last_flush
        if len(self._buffer) >= self._max_chars or elapsed >= self._max_interval:
            await self.flush()

    async def flush(self) -> None:
        if not self._buffer:
            return
        await self._emit(self._buffer, self._index)
        self._index += 1
        self._buffer = ""
        self._last_flush = time.monotonic()


class LLMProvider(ABC):
    """Interface for LLM providers."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Return the provider-owned model identifier."""

    @abstractmethod
    async def send(self, request: LLMRequest) -> LLMResponse:
        """Send a normalized request to the provider."""

    @abstractmethod
    def set_event_logger(
        self, logger: EventLogger, session_id: str, app_id: str
    ) -> None:
        """Set the event logger for LLM operations."""

    @abstractmethod
    def set_trace_id(self, trace_id: Optional[str]) -> None:
        """Set the trace ID for the current agent execution."""

    def get_event_logger_session_id(self) -> Optional[str]:
        """Return the currently bound event-logger session ID, if any."""
        return None

    def enable_thought_summaries(self) -> None:
        """Enable exposed textual summaries when supported; otherwise a no-op."""

    async def close(self) -> None:
        """Release provider resources. Default is no-op."""

    def capabilities(self) -> LLMCapabilities:
        """Return optional capabilities beyond the core provider contract."""
        return LLMCapabilities()


class BaseLLMProvider(LLMProvider):
    """Shared provider plumbing for logging and normalized usage."""

    provider_name = "unknown"

    def __init__(
        self,
        *,
        app_id: str,
        model: str,
        event_logger: Optional[EventLogger] = None,
        session_id: Optional[str] = None,
    ) -> None:
        cleaned_model = model.strip()
        if not cleaned_model:
            raise ValueError("model is required")
        self._event_logger = event_logger
        self._session_id = session_id
        self._app_id = app_id
        self._model = cleaned_model
        self._trace_id: Optional[str] = None

    @property
    def model(self) -> str:
        return self._model

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        self._trace_id = trace_id

    def set_event_logger(
        self, logger: EventLogger, session_id: str, app_id: str
    ) -> None:
        self._event_logger = logger
        self._session_id = session_id
        self._app_id = app_id

    def get_event_logger_session_id(self) -> Optional[str]:
        return self._session_id

    def _request_betas(self, request: LLMRequest) -> Optional[List[str]]:
        betas = request.provider_options.get("betas")
        return betas if isinstance(betas, list) else None

    def _delta_stream(self, request: LLMRequest) -> _DeltaStream:
        """Create a coalescing delta stream for a streamed request."""
        async def _emit(text: str, index: int) -> None:
            await self._emit_response_delta(request, text=text, index=index)
        return _DeltaStream(_emit)

    async def _emit_response_delta(
        self,
        request: LLMRequest,
        *,
        text: str,
        index: int,
    ) -> None:
        """Emit one coalesced ``chat.delta`` chunk of a streamed model call.

        The chunk lives in ``payload`` as ``{"text", "index"}``. Inside a
        request the row is keyed by the bound loop step and the chunk index,
        so a replayed model call keeps the first attempt's chunks and writes
        none of its own.
        """
        if self._event_logger is None:
            return

        loop_index = get_loop_index()
        await self._event_logger.emit(
            LLMEvent(
                event_type="chat.delta",
                app_id=self._app_id,
                session_id=self._session_id,
                provider=self.provider_name,
                model=request.model,
                trace_id=self._trace_id,
                payload={"text": text, "index": index},
                dedupe_key=(
                    f"chat.delta.{loop_index}.{index}" if loop_index is not None else None
                ),
            )
        )

    def _log_request_error(self, error: Exception) -> None:
        log.warning(
            "llm request error [%s/%s]: %s",
            self.provider_name,
            self._app_id,
            error,
        )
