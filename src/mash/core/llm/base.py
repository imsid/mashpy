"""Provider-neutral LLM interfaces and shared base implementation."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from ...logging import EventLogger, LLMEvent
from .types import (
    LLMCapabilities,
    LLMRequest,
    LLMResponse,
    LLMTokenUsage,
    LLMToolDefinition,
)


class LLMProvider(ABC):
    """Interface for LLM providers."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Return the provider-owned model identifier."""

    @abstractmethod
    def send(self, request: LLMRequest) -> LLMResponse:
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

    def _emit_request_start(
        self,
        request: LLMRequest,
        *,
        payload: Dict[str, Any] = {},
    ) -> None:
        if self._event_logger is None:
            return

        self._event_logger.emit(
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

    def _emit_request_complete(
        self,
        request: LLMRequest,
        *,
        started_at: float,
        response: LLMResponse,
    ) -> None:
        if self._event_logger is None:
            return

        usage = response.usage or LLMTokenUsage()
        self._event_logger.emit(
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

    def _emit_request_error(
        self,
        request: LLMRequest,
        *,
        started_at: float,
        error: Exception,
    ) -> None:
        if self._event_logger is None:
            return

        self._event_logger.emit(
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
