"""Shared helpers for Mash API route modules."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import Request
from pydantic import BaseModel, Field

from mash.api.logging import build_api_event_filter
from mash.logging.logger import EventLogger
from mash.memory.search.service import MemorySearchService
from mash.memory.search.types import FusionWeights, RetrievalConfig
from mash.runtime import AgentPool
from mash.runtime.client import AgentClientLike

# Cookie the admin SPA sets so its same-origin API calls authenticate without
# an explicit Authorization header.
API_KEY_COOKIE = "mash_api_key"


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
class AppRuntimeState:
    pool: AgentPool
    api_event_store: Any
    eval_service: Any
    api_key: Optional[str]
    observability_enabled: bool
    default_events_limit: int
    default_search_limit: int


class SubmitRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    structured_output: Optional[dict[str, Any]] = None


class DefineHostRequest(BaseModel):
    primary: str = Field(min_length=1)
    subagents: list[str] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)


class HostSubmitRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    structured_output: Optional[dict[str, Any]] = None
    context: Optional[str] = None


class CompactSessionRequest(BaseModel):
    reason: str = "manual"
    session_total_tokens_reset: int = 0


class RegisterAgentSkillRequest(BaseModel):
    type: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = ""
    location: Optional[str] = None
    content: Optional[str] = None


class DynamicTaskSpecRequest(BaseModel):
    task_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    structured_output: Optional[dict[str, Any]] = None


class WorkflowTaskMessageRequest(BaseModel):
    skill_name: str = Field(min_length=1)


class RegisterAgentWorkflowRequest(BaseModel):
    workflow_id: str = Field(min_length=1)
    tasks: list[DynamicTaskSpecRequest] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    task_message: WorkflowTaskMessageRequest


class RunWorkflowRequest(BaseModel):
    dedup_key: Optional[str] = None
    input: dict[str, Any] = Field(default_factory=dict)
    session_id: Optional[str] = None


class CommandEventIngest(BaseModel):
    """One CLI command lifecycle event shipped from the REPL."""

    agent_id: str
    event_type: str
    session_id: Optional[str] = None
    host_id: Optional[str] = None
    command_name: Optional[str] = None
    args: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    trace_id: Optional[str] = None
    ts: Optional[float] = None


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


def success(data: Any) -> dict[str, Any]:
    return {"data": data}


def error_payload(
    code: str,
    message: str,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def state_from_request(request: Request) -> AppRuntimeState:
    state = getattr(request.app.state, "runtime_state", None)
    if state is None:
        raise APIError(
            code="RUNTIME_NOT_READY",
            message="runtime is not initialized",
            status_code=503,
        )
    return state


def normalize_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    return text or None


def require_message(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise APIError(code="INVALID_REQUEST", message="message is required", status_code=400)
    return text


def require_session_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise APIError(code="INVALID_REQUEST", message="session_id is required", status_code=400)
    return text


def require_trace_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise APIError(code="INVALID_REQUEST", message="trace_id is required", status_code=400)
    return text


def require_agent_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise APIError(code="INVALID_REQUEST", message="agent_id is required", status_code=400)
    return text


def parse_limit(raw: Optional[int], *, default: int, max_value: int) -> int:
    if raw is None:
        return default
    return max(1, min(int(raw), max_value))


def build_runtime_event_sse_payload(event_name: str, payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=True)
    return f"event: {event_name}\ndata: {data}\n\n"


def build_observability_sse_payload(payload: str) -> str:
    return f"data: {payload}\n\n"


def get_client(request: Request, agent_id: str) -> AgentClientLike:
    state = state_from_request(request)
    try:
        return state.pool.get_client(agent_id.strip())
    except ValueError as exc:
        raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc


def get_agent(request: Request, agent_id: str):
    state = state_from_request(request)
    try:
        return state.pool.get_agent(agent_id.strip())
    except ValueError as exc:
        raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc


def get_workflow_service(request: Request):
    state = state_from_request(request)
    return state.pool.get_workflow_service()


def get_eval_service(request: Request):
    state = state_from_request(request)
    if state.eval_service is None:
        raise APIError(
            code="EVALS_NOT_AVAILABLE",
            message="evals require MASH_DATABASE_URL to be configured",
            status_code=503,
        )
    return state.eval_service


def telemetry_event_source() -> str:
    return "runtime_event_log"


def api_event_source() -> str:
    return "api_event_log"


def build_memory_search_service(agent: Any) -> MemorySearchService:
    return MemorySearchService(
        agent.memory_store,
        event_logger=EventLogger(agent.runtime_store),
        retrieval_config=RetrievalConfig(enable_keyword=True, enable_semantic=False),
        fusion_weights=FusionWeights(keyword_weight=1.0, semantic_weight=0.0),
    )


def memory_search_available(agent: Any) -> bool:
    return hasattr(agent, "memory_store") and hasattr(agent, "runtime_store")


def build_api_filters(**kwargs: Any):
    return build_api_event_filter(**kwargs)


def api_key_from_request(request: Request) -> Optional[str]:
    header_token: Optional[str] = None
    auth_header = request.headers.get("authorization")
    if isinstance(auth_header, str) and auth_header.lower().startswith("bearer "):
        header_token = auth_header[7:].strip() or None

    x_api_key = normalize_optional_text(request.headers.get("x-api-key"))
    cookie_api_key = normalize_optional_text(request.cookies.get(API_KEY_COOKIE))
    return header_token or x_api_key or cookie_api_key
