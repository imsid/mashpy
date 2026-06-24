"""Provider-neutral LLM interfaces and shared base implementation."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

from ...logging import EventLogger, LLMEvent
from .types import (
    LLMCapabilities,
    LLMRequest,
    LLMResponse,
    LLMTokenUsage,
    LLMToolDefinition,
)


# Coalescing thresholds for streamed response deltas. Deltas are flushed into a
# single ``llm.response.delta`` event when the buffer reaches this many
# characters, or this many seconds elapse since the last flush (whichever comes
# first). This bounds event volume (~tens per turn instead of one per token
# chunk) while keeping the visible stream responsive.
DEFAULT_DELTA_MAX_CHARS = 80
DEFAULT_DELTA_MAX_INTERVAL = 0.5


class _DeltaStream:
    """Coalesces streamed text deltas into bounded ``llm.response.delta`` events.

    Providers push raw text chunks via :meth:`push`; the stream flushes a
    coalesced delta event when the buffer crosses the size or time threshold,
    and :meth:`flush` emits any trailing partial buffer when the stream ends.
    """

    def __init__(
        self,
        provider: "BaseLLMProvider",
        request: LLMRequest,
        *,
        max_chars: int = DEFAULT_DELTA_MAX_CHARS,
        max_interval: float = DEFAULT_DELTA_MAX_INTERVAL,
    ) -> None:
        self._provider = provider
        self._request = request
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
        await self._provider._emit_response_delta(
            self._request, text=self._buffer, index=self._index
        )
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

    def _tool_names(self, tools: List[LLMToolDefinition]) -> List[str]:
        return [tool.name for tool in tools if tool.name]

    def _request_betas(self, request: LLMRequest) -> Optional[List[str]]:
        betas = request.provider_options.get("betas")
        return betas if isinstance(betas, list) else None

    async def _emit_request_start(
        self,
        request: LLMRequest,
        *,
        payload: Dict[str, Any] = {},
    ) -> None:
        if self._event_logger is None:
            return

        await self._event_logger.emit(
            LLMEvent(
                event_type="llm.request.start",
                app_id=self._app_id,
                session_id=self._session_id,
                provider=self.provider_name,
                model=request.model,
                trace_id=self._trace_id,
                tools=self._tool_names(request.tools),
                payload=payload,
                betas=self._request_betas(request),
            )
        )

    async def _emit_request_complete(
        self,
        request: LLMRequest,
        *,
        started_at: float,
        response: LLMResponse,
    ) -> None:
        if self._event_logger is None:
            return

        usage = response.usage or LLMTokenUsage()
        await self._event_logger.emit(
            LLMEvent(
                event_type="llm.request.complete",
                app_id=self._app_id,
                session_id=self._session_id,
                provider=self.provider_name,
                model=request.model,
                duration_ms=int((time.time() - started_at) * 1000),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cache_creation_input_tokens=usage.cache_write_tokens,
                cache_read_input_tokens=usage.cache_read_tokens,
                finish_reason=response.stop_reason,
                metadata=response.provider_metadata or None,
                trace_id=self._trace_id,
                tools=self._tool_names(request.tools),
                betas=self._request_betas(request),
            )
        )

    def _delta_stream(self, request: LLMRequest) -> _DeltaStream:
        """Create a coalescing delta stream for a streamed request."""
        return _DeltaStream(self, request)

    async def _emit_response_delta(
        self,
        request: LLMRequest,
        *,
        text: str,
        index: int,
    ) -> None:
        """Emit one incremental ``llm.response.delta`` event.

        Correlates with ``llm.request.start`` / ``llm.request.complete`` via the
        provider's bound trace/session/app ids. The coalesced text chunk lives
        in ``payload`` so consumers can render it live.
        """
        if self._event_logger is None:
            return

        await self._event_logger.emit(
            LLMEvent(
                event_type="llm.response.delta",
                app_id=self._app_id,
                session_id=self._session_id,
                provider=self.provider_name,
                model=request.model,
                trace_id=self._trace_id,
                payload={"text": text, "index": index},
            )
        )

    async def _emit_request_error(
        self,
        request: LLMRequest,
        *,
        started_at: float,
        error: Exception,
    ) -> None:
        log.warning(
            "llm request error [%s/%s]: %s",
            self.provider_name,
            self._app_id,
            error,
        )
        if self._event_logger is None:
            return
        try:
            await self._event_logger.emit(
                LLMEvent(
                    event_type="llm.request.error",
                    app_id=self._app_id,
                    session_id=self._session_id,
                    provider=self.provider_name,
                    model=request.model,
                    error=str(error),
                    duration_ms=int((time.time() - started_at) * 1000),
                    trace_id=self._trace_id,
                    tools=self._tool_names(request.tools),
                    betas=self._request_betas(request),
                )
            )
        except Exception as store_exc:  # pylint: disable=broad-except
            log.warning("failed to store llm.request.error event: %s", store_exc)
