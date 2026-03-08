"""FastAPI composition for mash-api runtime and telemetry endpoints."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional, Sequence

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from mash.logging.logger import EventLogger
from mash.memory.search.service import MemorySearchService
from mash.memory.search.types import FusionWeights, RetrievalConfig
from mash.memory.store import SQLiteStore
from mash.runtime import MashAgentClient, MashAgentClientError, MashAgentHost, MashRuntimeDefinition

from .config import MashAPIConfig
from .types import SubagentRegistration


class APIError(RuntimeError):
    """Structured API error for envelope serialization."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


@dataclass
class _AppRuntimeState:
    host: MashAgentHost
    primary_agent_id: str
    primary_client: MashAgentClient
    api_key: Optional[str]
    observability_enabled: bool
    observability_log_path: Optional[Path]
    observability_memory_db_path: Optional[Path]
    search_service: Optional[MemorySearchService]
    default_events_limit: int
    default_search_limit: int


class InvokeRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: Optional[str] = None
    turn_metadata: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: Optional[int] = None


class SubmitRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: Optional[str] = None
    turn_metadata: dict[str, Any] = Field(default_factory=dict)


class PreferencesUpdateRequest(BaseModel):
    preferences: dict[str, Any]


class AppDataSetRequest(BaseModel):
    value: Any


class CompactSessionRequest(BaseModel):
    reason: str = "manual"
    session_total_tokens_reset: int = 0


def _success(data: Any) -> dict[str, Any]:
    return {"data": data}


def _error_payload(code: str, message: str, details: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        }
    }


def _state_from_request(request: Request) -> _AppRuntimeState:
    state = getattr(request.app.state, "runtime_state", None)
    if state is None:
        raise APIError(
            code="RUNTIME_NOT_READY",
            message="runtime is not initialized",
            status_code=503,
        )
    return state


def _normalize_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _require_message(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise APIError(code="INVALID_REQUEST", message="message is required", status_code=400)
    return text


def _parse_limit(raw: Optional[int], *, default: int, max_value: int) -> int:
    if raw is None:
        return default
    return max(1, min(int(raw), max_value))


def _build_runtime_event_sse_payload(event_name: str, payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=True)
    return f"event: {event_name}\ndata: {data}\n\n"


def _build_observability_sse_payload(payload: str) -> str:
    return f"data: {payload}\n\n"


def create_app(
    definition: MashRuntimeDefinition,
    *,
    subagents: Sequence[SubagentRegistration] | None = None,
    config: MashAPIConfig | None = None,
) -> FastAPI:
    """Build a FastAPI app that composes Mash runtime + observability APIs."""

    resolved_config = config or MashAPIConfig()

    @asynccontextmanager
    async def _lifespan(application: FastAPI):
        host = MashAgentHost(bind_host=resolved_config.runtime_bind_host)
        primary_agent_id = host.register_primary(definition)
        for registration in subagents or ():
            host.register_subagent(
                registration.definition,
                metadata=registration.metadata,
                agent_id=registration.agent_id,
            )
        host.start()
        primary_client = host.get_client(primary_agent_id)

        default_log_path = definition.get_log_destination().expanduser().resolve()
        observability_log_path = resolved_config.resolved_log_path() or default_log_path
        observability_memory_db_path = resolved_config.resolved_memory_db_path()

        search_service: Optional[MemorySearchService] = None
        if resolved_config.enable_observability and observability_memory_db_path is not None:
            event_logger = EventLogger(observability_log_path)
            search_service = MemorySearchService(
                SQLiteStore(observability_memory_db_path),
                event_logger=event_logger,
                retrieval_config=RetrievalConfig(enable_keyword=True, enable_semantic=False),
                fusion_weights=FusionWeights(keyword_weight=1.0, semantic_weight=0.0),
            )

        application.state.runtime_state = _AppRuntimeState(
            host=host,
            primary_agent_id=primary_agent_id,
            primary_client=primary_client,
            api_key=resolved_config.resolved_api_key(),
            observability_enabled=resolved_config.enable_observability,
            observability_log_path=observability_log_path,
            observability_memory_db_path=observability_memory_db_path,
            search_service=search_service,
            default_events_limit=max(1, int(resolved_config.default_events_limit)),
            default_search_limit=max(1, int(resolved_config.default_search_limit)),
        )
        try:
            yield
        finally:
            state = getattr(application.state, "runtime_state", None)
            if state is not None:
                state.host.close()
            application.state.runtime_state = None

    app = FastAPI(title="Mash API", version="1.0.0", lifespan=_lifespan)

    cors_origins = resolved_config.resolved_cors_origins()
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=False,
        )

    @app.exception_handler(APIError)
    async def _api_error_handler(_: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                "VALIDATION_ERROR",
                "request validation failed",
                {"errors": exc.errors()},
            ),
        )

    @app.exception_handler(MashAgentClientError)
    async def _client_error_handler(_: Request, exc: MashAgentClientError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content=_error_payload("RUNTIME_CLIENT_ERROR", str(exc)),
        )

    async def _authorize(request: Request) -> None:
        state = _state_from_request(request)
        expected_key = state.api_key
        if expected_key is None:
            return

        header_token: Optional[str] = None
        auth_header = request.headers.get("authorization")
        if isinstance(auth_header, str) and auth_header.lower().startswith("bearer "):
            header_token = auth_header[7:].strip() or None

        x_api_key = _normalize_optional_text(request.headers.get("x-api-key"))
        provided = header_token or x_api_key
        if provided != expected_key:
            raise APIError(
                code="UNAUTHORIZED",
                message="valid API key is required",
                status_code=401,
            )

    api = APIRouter(prefix=resolved_config.api_prefix, dependencies=[Depends(_authorize)])

    @api.get("/health")
    def health(request: Request) -> dict[str, Any]:
        state = _state_from_request(request)
        session_info = state.primary_client.get_session_info()
        log_path = state.observability_log_path
        return _success(
            {
                "status": "ok",
                "api_version": "v1",
                "runtime": {
                    "primary_agent_id": state.primary_agent_id,
                    "app_id": session_info.get("app_id"),
                    "session_id": session_info.get("session_id"),
                    "subagent_ids": session_info.get("subagent_ids", []),
                    "model": session_info.get("model"),
                    "max_steps": session_info.get("max_steps"),
                },
                "observability": {
                    "enabled": state.observability_enabled,
                    "events": {
                        "configured": log_path is not None,
                        "path": str(log_path) if log_path is not None else None,
                        "exists": bool(log_path.exists()) if log_path is not None else False,
                        "default_limit": state.default_events_limit,
                    },
                    "memory": {
                        "configured": state.observability_memory_db_path is not None,
                        "search_available": state.search_service is not None,
                        "path": (
                            str(state.observability_memory_db_path)
                            if state.observability_memory_db_path is not None
                            else None
                        ),
                        "default_limit": state.default_search_limit,
                    },
                },
            }
        )

    @api.post("/interactions/invoke")
    def invoke(request: Request, body: InvokeRequest) -> dict[str, Any]:
        state = _state_from_request(request)
        message = _require_message(body.message)
        session_id = _normalize_optional_text(body.session_id)
        timeout_ms = body.timeout_ms if body.timeout_ms is None else int(body.timeout_ms)
        if timeout_ms is not None and timeout_ms <= 0:
            timeout_ms = None

        try:
            result = state.primary_client.invoke(
                message,
                session_id=session_id,
                turn_metadata=dict(body.turn_metadata or {}),
                timeout_ms=timeout_ms,
            )
        except TimeoutError as exc:
            raise APIError(
                code="REQUEST_TIMEOUT",
                message=str(exc),
                status_code=504,
            ) from exc
        return _success(result)

    @api.post("/interactions/requests")
    def submit_request(request: Request, body: SubmitRequest) -> dict[str, Any]:
        state = _state_from_request(request)
        request_id = state.primary_client.post_request(
            _require_message(body.message),
            session_id=_normalize_optional_text(body.session_id),
            turn_metadata=dict(body.turn_metadata or {}),
        )
        return _success({"request_id": request_id})

    @api.get("/interactions/requests/{request_id}/events")
    def stream_request_events(request: Request, request_id: str) -> StreamingResponse:
        state = _state_from_request(request)
        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            raise APIError(
                code="INVALID_REQUEST",
                message="request_id is required",
                status_code=400,
            )

        def _generate() -> Iterator[str]:
            try:
                for event in state.primary_client.stream(normalized_request_id):
                    event_name = str(event.get("event") or "message")
                    payload = event.get("data")
                    yield _build_runtime_event_sse_payload(event_name, payload)
                    if event_name in {"request.completed", "request.error"}:
                        break
            except MashAgentClientError as exc:
                yield _build_runtime_event_sse_payload(
                    "request.error",
                    {
                        "request_id": normalized_request_id,
                        "status": "error",
                        "error": str(exc),
                    },
                )

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    @api.get("/runtime/session")
    def get_runtime_session(request: Request, session_id: Optional[str] = None) -> dict[str, Any]:
        state = _state_from_request(request)
        info = state.primary_client.get_session_info(_normalize_optional_text(session_id))
        return _success(info)

    @api.get("/runtime/subagents")
    def get_subagents(request: Request) -> dict[str, Any]:
        state = _state_from_request(request)
        return _success({"subagent_ids": state.primary_client.get_subagent_ids()})

    @api.get("/runtime/sessions/{session_id}/preferences")
    def get_preferences(request: Request, session_id: str) -> dict[str, Any]:
        state = _state_from_request(request)
        if not session_id.strip():
            raise APIError(code="INVALID_REQUEST", message="session_id is required", status_code=400)
        return _success({"preferences": state.primary_client.get_preferences(session_id)})

    @api.put("/runtime/sessions/{session_id}/preferences")
    def set_preferences(request: Request, session_id: str, body: PreferencesUpdateRequest) -> dict[str, Any]:
        state = _state_from_request(request)
        if not session_id.strip():
            raise APIError(code="INVALID_REQUEST", message="session_id is required", status_code=400)
        state.primary_client.set_preferences(session_id, body.preferences)
        return _success({"ok": True})

    @api.get("/runtime/sessions/{session_id}/app-data")
    def list_app_data(request: Request, session_id: str) -> dict[str, Any]:
        state = _state_from_request(request)
        if not session_id.strip():
            raise APIError(code="INVALID_REQUEST", message="session_id is required", status_code=400)
        return _success({"items": state.primary_client.list_app_data(session_id)})

    @api.get("/runtime/sessions/{session_id}/app-data/{key}")
    def get_app_data(request: Request, session_id: str, key: str) -> dict[str, Any]:
        state = _state_from_request(request)
        if not session_id.strip() or not key.strip():
            raise APIError(code="INVALID_REQUEST", message="session_id and key are required", status_code=400)
        return _success({"value": state.primary_client.get_app_data(session_id, key)})

    @api.put("/runtime/sessions/{session_id}/app-data/{key}")
    def set_app_data(request: Request, session_id: str, key: str, body: AppDataSetRequest) -> dict[str, Any]:
        state = _state_from_request(request)
        if not session_id.strip() or not key.strip():
            raise APIError(code="INVALID_REQUEST", message="session_id and key are required", status_code=400)
        state.primary_client.set_app_data(session_id, key, body.value)
        return _success({"ok": True})

    @api.delete("/runtime/sessions/{session_id}/app-data/{key}")
    def delete_app_data(request: Request, session_id: str, key: str) -> dict[str, Any]:
        state = _state_from_request(request)
        if not session_id.strip() or not key.strip():
            raise APIError(code="INVALID_REQUEST", message="session_id and key are required", status_code=400)
        deleted = state.primary_client.delete_app_data(session_id, key)
        return _success({"deleted": bool(deleted)})

    @api.get("/runtime/sessions/{session_id}/history")
    def get_history(
        request: Request,
        session_id: str,
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        state = _state_from_request(request)
        if not session_id.strip():
            raise APIError(code="INVALID_REQUEST", message="session_id is required", status_code=400)
        turns = state.primary_client.get_history_turns(session_id, limit=limit)
        return _success({"turns": turns})

    @api.post("/runtime/sessions/{session_id}/compact")
    def compact_session(
        request: Request,
        session_id: str,
        body: CompactSessionRequest,
    ) -> dict[str, Any]:
        state = _state_from_request(request)
        if not session_id.strip():
            raise APIError(code="INVALID_REQUEST", message="session_id is required", status_code=400)
        summary_text, turn_id = state.primary_client.compact_session(
            session_id=session_id,
            reason=body.reason,
            session_total_tokens_reset=body.session_total_tokens_reset,
        )
        return _success({"summary_text": summary_text, "turn_id": turn_id})

    @api.get("/telemetry/events")
    def get_observability_events(
        request: Request,
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        state = _state_from_request(request)
        if not state.observability_enabled:
            raise APIError(
                code="OBSERVABILITY_DISABLED",
                message="telemetry endpoints are disabled",
                status_code=503,
            )

        log_path = state.observability_log_path
        if log_path is None:
            raise APIError(
                code="OBSERVABILITY_EVENTS_UNCONFIGURED",
                message="telemetry log path is not configured",
                status_code=503,
            )
        if not log_path.exists():
            raise APIError(
                code="LOG_FILE_NOT_FOUND",
                message="log file not found",
                status_code=404,
                details={"path": str(log_path)},
            )

        resolved_limit = _parse_limit(limit, default=state.default_events_limit, max_value=20000)
        events: list[dict[str, Any]] = []
        with log_path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()

        for raw in lines[-resolved_limit:]:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)

        return _success({
            "events": events,
            "path": str(log_path),
            "limit": resolved_limit,
        })

    @api.get("/telemetry/events/stream")
    async def stream_observability_events(request: Request) -> StreamingResponse:
        state = _state_from_request(request)
        if not state.observability_enabled:
            raise APIError(
                code="OBSERVABILITY_DISABLED",
                message="telemetry endpoints are disabled",
                status_code=503,
            )

        log_path = state.observability_log_path
        if log_path is None:
            raise APIError(
                code="OBSERVABILITY_EVENTS_UNCONFIGURED",
                message="telemetry log path is not configured",
                status_code=503,
            )
        if not log_path.exists():
            raise APIError(
                code="LOG_FILE_NOT_FOUND",
                message="log file not found",
                status_code=404,
                details={"path": str(log_path)},
            )

        async def _generate() -> AsyncIterator[str]:
            with log_path.open("r", encoding="utf-8") as handle:
                handle.seek(0, os.SEEK_END)
                while True:
                    if await request.is_disconnected():
                        break
                    line = handle.readline()
                    if not line:
                        yield ": keep-alive\n\n"
                        await asyncio.sleep(0.25)
                        continue
                    payload = line.strip()
                    if not payload:
                        continue
                    try:
                        json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    yield _build_observability_sse_payload(payload)

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    @api.get("/telemetry/memory/search")
    def search_memory(
        request: Request,
        q: str,
        app_id: str,
        session_id: Optional[str] = None,
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        state = _state_from_request(request)
        if not state.observability_enabled:
            raise APIError(
                code="OBSERVABILITY_DISABLED",
                message="telemetry endpoints are disabled",
                status_code=503,
            )

        query_text = q.strip()
        if not query_text:
            raise APIError(
                code="MISSING_QUERY",
                message="q is required",
                status_code=400,
                details={"param": "q"},
            )

        app_id_value = app_id.strip()
        if not app_id_value:
            raise APIError(
                code="MISSING_APP_ID",
                message="app_id is required",
                status_code=400,
                details={"param": "app_id"},
            )

        if state.search_service is None:
            raise APIError(
                code="MEMORY_SEARCH_UNAVAILABLE",
                message="memory search unavailable (configure memory db path)",
                status_code=503,
            )

        resolved_limit = _parse_limit(limit, default=state.default_search_limit, max_value=50)
        normalized_session_id = _normalize_optional_text(session_id)
        try:
            results = state.search_service.search(
                query_text,
                limit=resolved_limit,
                session_id=normalized_session_id,
                app_id=app_id_value,
            )
        except ValueError as exc:
            raise APIError(
                code="SEARCH_VALIDATION_ERROR",
                message=str(exc),
                status_code=400,
            ) from exc
        except (NotImplementedError, RuntimeError) as exc:
            raise APIError(
                code="SEARCH_UNAVAILABLE",
                message=str(exc),
                status_code=503,
            ) from exc
        except Exception as exc:  # pragma: no cover
            raise APIError(
                code="SEARCH_FAILED",
                message=f"search failed: {exc}",
                status_code=500,
            ) from exc

        return _success(
            {
                "results": [asdict(result) for result in results],
                "app_id": app_id_value,
                "session_id": normalized_session_id,
                "query": query_text,
                "limit": resolved_limit,
            }
        )

    app.include_router(api)

    @app.get("/")
    def root() -> dict[str, Any]:
        return {
            "service": "mash-api",
            "api": {
                "version": "v1",
                "base": resolved_config.api_prefix,
                "openapi": "/openapi.json",
                "docs": "/docs",
            },
        }

    return app


def run_app(
    definition: MashRuntimeDefinition,
    *,
    subagents: Sequence[SubagentRegistration] | None = None,
    config: MashAPIConfig | None = None,
) -> None:
    """Run mash-api service with uvicorn."""
    resolved_config = config or MashAPIConfig()
    app = create_app(definition, subagents=subagents, config=resolved_config)
    uvicorn.run(app, host=resolved_config.bind_host, port=resolved_config.bind_port)
