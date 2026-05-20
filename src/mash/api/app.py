"""FastAPI composition for Mash host deployment and runtime APIs."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator, Optional

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from mash.api.logging import (
    PostgresAPIEventStore,
    build_api_event_filter,
    serialize_api_event,
)
from mash.api.middleware import APILoggingMiddleware
from mash.logging.logger import EventLogger
from mash.memory.search.service import MemorySearchService
from mash.memory.search.types import FusionWeights, RetrievalConfig
from mash.runtime import AgentClientError, AgentHost
from mash.runtime.client import AgentClientLike
from mash.runtime.events import build_reasoning_trace, serialize_runtime_event
from mash.workflows import DuplicateWorkflowRunError, WorkflowNotFoundError

from .config import MashHostConfig
from .telemetry_ui import TELEMETRY_API_KEY_COOKIE, mount_telemetry_ui


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
    host: AgentHost
    api_event_store: Any
    api_key: Optional[str]
    observability_enabled: bool
    default_events_limit: int
    default_search_limit: int


class SubmitRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


class CompactSessionRequest(BaseModel):
    reason: str = "manual"
    session_total_tokens_reset: int = 0


class RunWorkflowRequest(BaseModel):
    dedup_key: Optional[str] = None
    input: dict[str, Any] = Field(default_factory=dict)


class APIEventSearchRequest(BaseModel):
    method: Optional[str] = None
    path: Optional[str] = None
    path_prefix: Optional[str] = None
    status_code: Optional[int] = None
    status_code_min: Optional[int] = None
    status_code_max: Optional[int] = None
    from_ts: Optional[float] = None
    to_ts: Optional[float] = None
    after_event_id: int = 0
    limit: Optional[int] = None


def _success(data: Any) -> dict[str, Any]:
    return {"data": data}


def _error_payload(code: str, message: str, details: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def _state_from_request(request: Request) -> _AppRuntimeState:
    state = getattr(request.app.state, "runtime_state", None)
    if state is None:
        raise APIError(code="RUNTIME_NOT_READY", message="runtime is not initialized", status_code=503)
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


def _require_session_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise APIError(code="INVALID_REQUEST", message="session_id is required", status_code=400)
    return text


def _require_trace_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise APIError(code="INVALID_REQUEST", message="trace_id is required", status_code=400)
    return text


def _require_agent_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise APIError(code="INVALID_REQUEST", message="agent_id is required", status_code=400)
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


def _get_client(request: Request, agent_id: str) -> AgentClientLike:
    state = _state_from_request(request)
    try:
        return state.host.get_client(agent_id.strip())
    except ValueError as exc:
        raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc


def _get_agent(request: Request, agent_id: str):
    state = _state_from_request(request)
    try:
        return state.host.get_agent(agent_id.strip())
    except ValueError as exc:
        raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc


def _get_workflow_service(request: Request):
    state = _state_from_request(request)
    return state.host.get_workflow_service()


def _telemetry_event_source() -> str:
    return "runtime_event_log"


def _api_event_source() -> str:
    return "api_event_log"


def _build_memory_search_service(agent: Any) -> MemorySearchService:
    return MemorySearchService(
        agent.memory_store,
        event_logger=EventLogger(agent.runtime_store),
        retrieval_config=RetrievalConfig(enable_keyword=True, enable_semantic=False),
        fusion_weights=FusionWeights(keyword_weight=1.0, semantic_weight=0.0),
    )


def _memory_search_available(agent: Any) -> bool:
    return hasattr(agent, "memory_store") and hasattr(agent, "runtime_store")


def create_app(host: AgentHost, *, config: MashHostConfig | None = None) -> FastAPI:
    """Build a FastAPI app that exposes one hosted Mash deployment."""

    resolved_config = config or MashHostConfig()

    @asynccontextmanager
    async def _lifespan(application: FastAPI):
        host.configure_runtime_database_url(
            resolved_config.resolved_runtime_database_url()
        )
        await host.start()
        api_event_store = PostgresAPIEventStore(
            resolved_config.resolved_runtime_database_url() or ""
        )
        await api_event_store.open()
        application.state.runtime_state = _AppRuntimeState(
            host=host,
            api_event_store=api_event_store,
            api_key=resolved_config.resolved_api_key(),
            observability_enabled=resolved_config.enable_observability,
            default_events_limit=max(1, int(resolved_config.default_events_limit)),
            default_search_limit=max(1, int(resolved_config.default_search_limit)),
        )
        try:
            yield
        finally:
            state = getattr(application.state, "runtime_state", None)
            if state is not None:
                await state.api_event_store.close()
                await state.host.close()
            application.state.runtime_state = None

    app = FastAPI(title="Mash Host", version="1.0.0", lifespan=_lifespan)
    mount_telemetry_ui(app)

    cors_origins = resolved_config.resolved_cors_origins()
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=False,
        )

    app.add_middleware(APILoggingMiddleware, config=resolved_config)

    @app.exception_handler(APIError)
    async def _api_error_handler(_: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_error_payload(exc.code, exc.message, exc.details))

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_error_payload("VALIDATION_ERROR", "request validation failed", {"errors": exc.errors()}),
        )

    @app.exception_handler(AgentClientError)
    async def _client_error_handler(_: Request, exc: AgentClientError) -> JSONResponse:
        return JSONResponse(status_code=502, content=_error_payload("RUNTIME_CLIENT_ERROR", str(exc)))

    @app.exception_handler(WorkflowNotFoundError)
    async def _workflow_not_found_handler(_: Request, exc: WorkflowNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content=_error_payload("WORKFLOW_NOT_FOUND", str(exc)))

    @app.exception_handler(DuplicateWorkflowRunError)
    async def _duplicate_workflow_run_handler(_: Request, exc: DuplicateWorkflowRunError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=_error_payload(
                "WORKFLOW_DUPLICATE_RUN",
                str(exc),
                {"run_id": exc.existing_run.run_id},
            ),
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
        cookie_api_key = _normalize_optional_text(request.cookies.get(TELEMETRY_API_KEY_COOKIE))
        provided = header_token or x_api_key or cookie_api_key
        if provided != expected_key:
            raise APIError(code="UNAUTHORIZED", message="valid API key is required", status_code=401)

    api = APIRouter(prefix=resolved_config.api_prefix, dependencies=[Depends(_authorize)])

    @api.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        state = _state_from_request(request)
        primary_client = state.host.get_client(state.host.get_primary_agent_id())
        primary_agent = state.host.get_agent(state.host.get_primary_agent_id())
        health_payload = await primary_client.health()
        primary_info = health_payload.get("session") if isinstance(health_payload, dict) else {}
        return _success(
            {
                "status": "ok",
                "service": "mash-api",
                "api_version": "v1",
                "deployment": {
                    "primary_agent_id": state.host.get_primary_agent_id(),
                    "agents": state.host.describe_agents(),
                },
                "primary_agent": primary_info,
                "observability": {
                    "enabled": state.observability_enabled,
                    "memory": {
                        "search_available": (
                            state.observability_enabled and _memory_search_available(primary_agent)
                        ),
                        "default_limit": state.default_search_limit,
                    },
                },
            }
        )

    @api.get("/agent")
    async def list_agents(request: Request) -> dict[str, Any]:
        state = _state_from_request(request)
        return _success({"agents": state.host.describe_agents(), "primary_agent_id": state.host.get_primary_agent_id()})

    @api.get("/agent/{agent_id}")
    async def get_agent(request: Request, agent_id: str) -> dict[str, Any]:
        state = _state_from_request(request)
        client = _get_client(request, agent_id)
        described = {item["agent_id"]: item for item in state.host.describe_agents()}
        health_payload = await client.health()
        session_payload = health_payload.get("session") if isinstance(health_payload, dict) else {}
        return _success(
            {
                "agent": described.get(agent_id, {"agent_id": agent_id}),
                "session": session_payload,
            }
        )

    @api.post("/agent/{agent_id}/request")
    async def submit_request(request: Request, agent_id: str, body: SubmitRequest) -> dict[str, Any]:
        client = _get_client(request, agent_id)
        request_id = await client.post_request(
            _require_message(body.message),
            session_id=_require_session_id(body.session_id),
        )
        return _success({"request_id": request_id})

    @api.get("/agent/{agent_id}/request/{request_id}/events")
    async def stream_request_events(request: Request, agent_id: str, request_id: str) -> StreamingResponse:
        client = _get_client(request, agent_id)
        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            raise APIError(code="INVALID_REQUEST", message="request_id is required", status_code=400)

        async def _generate() -> AsyncIterator[str]:
            try:
                async for event in client.stream_response(normalized_request_id):
                    event_name = str(event.get("event") or "message")
                    payload = event.get("data")
                    yield _build_runtime_event_sse_payload(event_name, payload)
                    if event_name in {"request.completed", "request.error"}:
                        break
            except AgentClientError as exc:
                yield _build_runtime_event_sse_payload(
                    "request.error",
                    {"request_id": normalized_request_id, "status": "error", "error": str(exc)},
                )

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @api.get("/agent/{agent_id}/sessions")
    async def list_sessions(request: Request, agent_id: str) -> dict[str, Any]:
        agent = _get_agent(request, agent_id)
        return _success({"sessions": await agent.list_sessions()})

    @api.get("/agent/{agent_id}/sessions/{session_id}")
    async def get_runtime_session(request: Request, agent_id: str, session_id: str) -> dict[str, Any]:
        agent = _get_agent(request, agent_id)
        return _success(await agent.get_session_info(_normalize_optional_text(session_id)))

    @api.get("/agent/{agent_id}/sessions/{session_id}/history")
    async def get_history(
        request: Request,
        agent_id: str,
        session_id: str,
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        agent = _get_agent(request, agent_id)
        if not session_id.strip():
            raise APIError(code="INVALID_REQUEST", message="session_id is required", status_code=400)
        return _success({"turns": await agent.get_history_turns(session_id, limit=limit)})

    @api.get("/agent/{agent_id}/sessions/{session_id}/signals")
    async def get_session_signals(
        request: Request,
        agent_id: str,
        session_id: str,
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        agent = _get_agent(request, agent_id)
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise APIError(code="INVALID_REQUEST", message="session_id is required", status_code=400)
        return _success(
            {
                "agent_id": agent.app_id,
                "session_id": normalized_session_id,
                "definitions": agent.get_signal_definitions(),
                "turns": await agent.get_session_signals(normalized_session_id, limit=limit),
            }
        )

    @api.post("/agent/{agent_id}/sessions/{session_id}/compact")
    async def compact_session(
        request: Request,
        agent_id: str,
        session_id: str,
        body: CompactSessionRequest,
    ) -> dict[str, Any]:
        agent = _get_agent(request, agent_id)
        if not session_id.strip():
            raise APIError(code="INVALID_REQUEST", message="session_id is required", status_code=400)
        summary_text, turn_id = await agent.compact_session(
            session_id=session_id,
            reason=body.reason,
            session_total_tokens_reset=body.session_total_tokens_reset,
        )
        return _success({"summary_text": summary_text, "turn_id": turn_id})

    @api.get("/workflows")
    async def list_workflows(request: Request) -> dict[str, Any]:
        workflow_service = _get_workflow_service(request)
        return _success({"workflows": await workflow_service.list_workflows()})

    @api.post("/workflows/{workflow_id}/run")
    async def run_workflow(
        request: Request,
        workflow_id: str,
        body: RunWorkflowRequest,
    ) -> dict[str, Any]:
        workflow_service = _get_workflow_service(request)
        try:
            run = await workflow_service.run_workflow(
                workflow_id.strip(),
                dedup_key=_normalize_optional_text(body.dedup_key),
                workflow_input=body.input,
            )
        except (WorkflowNotFoundError, DuplicateWorkflowRunError):
            raise
        except Exception as exc:
            raise APIError(
                code="WORKFLOW_RUN_FAILED",
                message=str(exc),
                status_code=500,
            ) from exc
        return _success(
            {
                "run_id": run.run_id,
                "workflow_id": run.workflow_id,
                "status": run.status,
            }
        )

    @api.get("/workflows/{workflow_id}/runs")
    async def list_workflow_runs(
        request: Request,
        workflow_id: str,
        status: Optional[str] = Query(default=None),
        start_time: Optional[str] = Query(default=None),
        end_time: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=50),
        offset: Optional[int] = Query(default=0),
        sort_desc: bool = Query(default=True),
    ) -> dict[str, Any]:
        workflow_service = _get_workflow_service(request)
        resolved_limit = _parse_limit(limit, default=50, max_value=200)
        resolved_offset = max(0, int(offset or 0))
        runs = await workflow_service.list_runs(
            workflow_id.strip(),
            status=status,
            start_time=start_time,
            end_time=end_time,
            limit=resolved_limit,
            offset=resolved_offset,
            sort_desc=sort_desc,
        )
        return _success(
            {
                "workflow_id": workflow_id.strip(),
                "runs": [
                    {
                        "run_id": run.run_id,
                        "workflow_id": run.workflow_id,
                        "dedup_key": run.dedup_key,
                        "status": run.status,
                        "created_at": run.created_at,
                        "started_at": run.started_at,
                        "finished_at": run.finished_at,
                        "error": run.error,
                    }
                    for run in runs
                ],
            }
        )

    @api.get("/workflows/{workflow_id}/runs/{run_id}")
    async def get_workflow_run(
        request: Request,
        workflow_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        workflow_service = _get_workflow_service(request)
        run = await workflow_service.get_run(workflow_id.strip(), run_id.strip())
        return _success(
            {
                "run_id": run.run_id,
                "workflow_id": run.workflow_id,
                "dedup_key": run.dedup_key,
                "status": run.status,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "error": run.error,
                "output": run.output,
            }
        )

    @api.get("/workflows/{workflow_id}/runs/{run_id}/events")
    async def stream_workflow_run_events(
        request: Request,
        workflow_id: str,
        run_id: str,
    ) -> StreamingResponse:
        workflow_service = _get_workflow_service(request)
        events = await workflow_service.stream_run_events(
            workflow_id.strip(),
            run_id.strip(),
        )

        async def _generate() -> AsyncIterator[str]:
            try:
                async for event in events:
                    if await request.is_disconnected():
                        break
                    if event.comment:
                        yield f": {event.comment}\n\n"
                        continue
                    yield _build_runtime_event_sse_payload(event.event, event.data)
            except Exception as exc:
                yield _build_runtime_event_sse_payload(
                    "workflow.error",
                    {"workflow_id": workflow_id, "run_id": run_id, "error": str(exc)},
                )

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @api.get("/telemetry/events")
    async def get_observability_events(
        request: Request,
        agent_id: str,
        session_id: Optional[str] = Query(default=None),
        trace_id: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        state = _state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

        try:
            agent = state.host.get_agent(agent_id)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

        resolved_limit = _parse_limit(limit, default=state.default_events_limit, max_value=20000)
        events = [
            serialize_runtime_event(item)
            for item in await agent.runtime_store.list_events(
                app_id=agent_id,
                session_id=_normalize_optional_text(session_id),
                trace_id=_normalize_optional_text(trace_id),
                limit=resolved_limit,
            )
        ]
        return _success(
            {
                "events": events,
                "source": _telemetry_event_source(),
                "agent_id": agent_id,
                "session_id": _normalize_optional_text(session_id),
                "trace_id": _normalize_optional_text(trace_id),
                "limit": resolved_limit,
            }
        )

    @api.get("/agent/{agent_id}/session/{session_id}/trace/{trace_id}/reasoning")
    async def get_reasoning_trace(
        request: Request,
        agent_id: str,
        session_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        state = _state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

        resolved_agent_id = _require_agent_id(agent_id)
        resolved_session_id = _require_session_id(session_id)
        resolved_trace_id = _require_trace_id(trace_id)
        try:
            agent = state.host.get_agent(resolved_agent_id)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

        events = await agent.runtime_store.list_events(
            app_id=resolved_agent_id,
            session_id=resolved_session_id,
            trace_id=resolved_trace_id,
        )
        trace_payload = build_reasoning_trace(events)
        return _success(
            {
                "source": _telemetry_event_source(),
                "agent_id": resolved_agent_id,
                "session_id": resolved_session_id,
                "trace_id": resolved_trace_id,
                **trace_payload,
            }
        )

    @api.get("/telemetry/events/stream")
    async def stream_observability_events(
        request: Request,
        agent_id: str,
        session_id: Optional[str] = Query(default=None),
        trace_id: Optional[str] = Query(default=None),
    ) -> StreamingResponse:
        state = _state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

        try:
            agent = state.host.get_agent(agent_id)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

        async def _generate() -> AsyncIterator[str]:
            resolved_session_id = _normalize_optional_text(session_id)
            resolved_trace_id = _normalize_optional_text(trace_id)
            latest = await agent.runtime_store.list_events(
                app_id=agent_id,
                session_id=resolved_session_id,
                trace_id=resolved_trace_id,
                limit=1,
            )
            last_seen = 0
            if latest:
                try:
                    last_seen = int(latest[-1].event_id or 0)
                except (TypeError, ValueError):
                    last_seen = 0

            while True:
                if await request.is_disconnected():
                    break
                events = await agent.runtime_store.list_events(
                    app_id=agent_id,
                    session_id=resolved_session_id,
                    trace_id=resolved_trace_id,
                    after_event_id=last_seen,
                )
                if not events:
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(0.25)
                    continue
                for event in events:
                    try:
                        last_seen = max(last_seen, int(event.event_id or 0))
                    except (TypeError, ValueError):
                        pass
                    yield _build_observability_sse_payload(
                        json.dumps(serialize_runtime_event(event), ensure_ascii=True)
                    )

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @api.get("/telemetry/api/events")
    async def get_api_events(
        request: Request,
        method: Optional[str] = Query(default=None),
        path: Optional[str] = Query(default=None),
        status_code: Optional[int] = Query(default=None),
        from_ts: Optional[float] = Query(default=None),
        to_ts: Optional[float] = Query(default=None),
        limit: Optional[int] = Query(default=None),
        after_event_id: int = Query(default=0),
    ) -> dict[str, Any]:
        state = _state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)
        filters = build_api_event_filter(
            method=method,
            path=path,
            status_code=status_code,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=limit or state.default_events_limit,
            after_event_id=after_event_id,
        )
        events = [serialize_api_event(item) for item in await state.api_event_store.list_events(filters)]
        return _success(
            {
                "events": events,
                "source": _api_event_source(),
                "limit": filters.limit,
            }
        )

    @api.post("/telemetry/api/events/search")
    async def search_api_events(request: Request, body: APIEventSearchRequest) -> dict[str, Any]:
        state = _state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)
        filters = build_api_event_filter(
            method=body.method,
            path=body.path,
            path_prefix=body.path_prefix,
            status_code=body.status_code,
            status_code_min=body.status_code_min,
            status_code_max=body.status_code_max,
            from_ts=body.from_ts,
            to_ts=body.to_ts,
            limit=body.limit or state.default_events_limit,
            after_event_id=body.after_event_id,
        )
        events = [serialize_api_event(item) for item in await state.api_event_store.list_events(filters)]
        return _success(
            {
                "events": events,
                "source": _api_event_source(),
                "limit": filters.limit,
            }
        )

    @api.get("/telemetry/api/events/stream")
    async def stream_api_events(
        request: Request,
        method: Optional[str] = Query(default=None),
        path: Optional[str] = Query(default=None),
        status_code: Optional[int] = Query(default=None),
        from_ts: Optional[float] = Query(default=None),
        to_ts: Optional[float] = Query(default=None),
    ) -> StreamingResponse:
        state = _state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

        async def _generate() -> AsyncIterator[str]:
            latest_filters = build_api_event_filter(
                method=method,
                path=path,
                status_code=status_code,
                from_ts=from_ts,
                to_ts=to_ts,
                limit=1,
            )
            latest = await state.api_event_store.list_events(latest_filters)
            last_seen = int(latest[-1].api_event_id) if latest else 0
            while True:
                if await request.is_disconnected():
                    break
                filters = build_api_event_filter(
                    method=method,
                    path=path,
                    status_code=status_code,
                    from_ts=from_ts,
                    to_ts=to_ts,
                    after_event_id=last_seen,
                    limit=state.default_events_limit,
                )
                events = await state.api_event_store.list_events(filters)
                if not events:
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(0.25)
                    continue
                for event in events:
                    last_seen = max(last_seen, int(event.api_event_id))
                    yield _build_observability_sse_payload(
                        json.dumps(serialize_api_event(event), ensure_ascii=True)
                    )

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @api.get("/telemetry/memory/search")
    async def search_memory(
        request: Request,
        q: str,
        app_id: str,
        session_id: Optional[str] = None,
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        state = _state_from_request(request)
        if not state.observability_enabled:
            raise APIError(code="OBSERVABILITY_DISABLED", message="telemetry endpoints are disabled", status_code=503)

        query_text = q.strip()
        if not query_text:
            raise APIError(code="MISSING_QUERY", message="q is required", status_code=400, details={"param": "q"})

        app_id_value = app_id.strip()
        if not app_id_value:
            raise APIError(
                code="MISSING_APP_ID",
                message="app_id is required",
                status_code=400,
                details={"param": "app_id"},
            )

        try:
            agent = state.host.get_agent(app_id_value)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

        if not _memory_search_available(agent):
            raise APIError(
                code="MEMORY_SEARCH_UNAVAILABLE",
                message="memory search unavailable for this agent",
                status_code=503,
            )

        search_service = _build_memory_search_service(agent)
        resolved_limit = _parse_limit(limit, default=state.default_search_limit, max_value=50)
        normalized_session_id = _normalize_optional_text(session_id)
        try:
            results = await search_service.search(
                query_text,
                limit=resolved_limit,
                session_id=normalized_session_id,
                app_id=app_id_value,
            )
        except ValueError as exc:
            raise APIError(code="SEARCH_VALIDATION_ERROR", message=str(exc), status_code=400) from exc
        except (NotImplementedError, RuntimeError) as exc:
            raise APIError(code="SEARCH_UNAVAILABLE", message=str(exc), status_code=503) from exc
        except Exception as exc:  # pragma: no cover
            raise APIError(code="SEARCH_FAILED", message=f"search failed: {exc}", status_code=500) from exc

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
    async def root() -> dict[str, Any]:
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


def run_host(host: AgentHost, *, config: MashHostConfig | None = None) -> None:
    """Run the Mash host API service with uvicorn."""

    resolved_config = config or MashHostConfig()
    app = create_app(host, config=resolved_config)
    uvicorn.run(app, host=resolved_config.bind_host, port=resolved_config.bind_port)
