"""Agent and session API routes."""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from mash.runtime import AgentClientError
from mash.runtime.events import build_reasoning_trace
from mash.runtime.structured_output import serialize_structured_output
from mash.skills import Skill
from mash.workflows import TaskSpec, WorkflowSpec, WorkflowTaskMessageSpec

from .common import (
    APIError,
    CompactSessionRequest,
    RegisterAgentSkillRequest,
    RegisterAgentWorkflowRequest,
    SubmitRequest,
    build_runtime_event_sse_payload,
    get_client,
    memory_search_available,
    normalize_optional_text,
    require_agent_id,
    require_message,
    require_session_id,
    require_trace_id,
    state_from_request,
    success,
    telemetry_event_source,
)
from .common import (
    get_agent as resolve_agent,
)


def build_agent_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        state = state_from_request(request)
        primary_client = state.host.get_client(state.host.get_primary_agent_id())
        primary_agent = state.host.get_agent(state.host.get_primary_agent_id())
        health_payload = await primary_client.health()
        primary_info = (
            health_payload.get("session") if isinstance(health_payload, dict) else {}
        )
        return success(
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
                            state.observability_enabled
                            and memory_search_available(primary_agent)
                        ),
                        "default_limit": state.default_search_limit,
                    },
                },
            }
        )

    @router.get("/agent")
    async def list_agents(request: Request) -> dict[str, Any]:
        state = state_from_request(request)
        return success(
            {
                "agents": state.host.describe_agents(),
                "primary_agent_id": state.host.get_primary_agent_id(),
            }
        )

    @router.get("/agent/{agent_id}")
    async def get_agent(request: Request, agent_id: str) -> dict[str, Any]:
        state = state_from_request(request)
        client = get_client(request, agent_id)
        described = {item["agent_id"]: item for item in state.host.describe_agents()}
        health_payload = await client.health()
        session_payload = (
            health_payload.get("session") if isinstance(health_payload, dict) else {}
        )
        return success(
            {
                "agent": described.get(agent_id, {"agent_id": agent_id}),
                "session": session_payload,
            }
        )

    @router.post("/agent/{agent_id}/skill")
    async def register_agent_skill(
        request: Request,
        agent_id: str,
        body: RegisterAgentSkillRequest,
    ) -> dict[str, Any]:
        state = state_from_request(request)
        resolved_agent_id = require_agent_id(agent_id)
        try:
            agent = state.host.get_agent(resolved_agent_id)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

        skill = Skill(
            type=body.type,
            name=body.name,
            description=body.description,
            location=body.location,
            content=body.content,
        )
        existing = agent.skills.get(skill.name)
        if existing is None:
            try:
                state.host.register_agent_skill(resolved_agent_id, skill)
            except ValueError as exc:
                raise APIError(
                    code="INVALID_AGENT_SKILL",
                    message=str(exc),
                    status_code=400,
                ) from exc
        return success({"agent_id": resolved_agent_id, "skill_name": skill.name})

    @router.post("/agent/{agent_id}/workflow")
    async def register_agent_workflow(
        request: Request,
        agent_id: str,
        body: RegisterAgentWorkflowRequest,
    ) -> dict[str, Any]:
        state = state_from_request(request)
        resolved_agent_id = require_agent_id(agent_id)
        if state.host.get_registered_agent_spec(resolved_agent_id) is None:
            raise APIError(
                code="AGENT_NOT_FOUND",
                message=f"agent '{resolved_agent_id}' is not registered",
                status_code=404,
            )

        workflow = WorkflowSpec(
            workflow_id=body.workflow_id,
            tasks=[
                TaskSpec(
                    task_id=task.task_id,
                    agent_id=task.agent_id,
                    structured_output=task.structured_output,
                )
                for task in body.tasks
            ],
            metadata=dict(body.metadata),
            task_message=WorkflowTaskMessageSpec(
                skill_name=body.task_message.skill_name,
            ),
        )
        try:
            state.host.register_agent_workflow(resolved_agent_id, workflow)
        except ValueError as exc:
            raise APIError(
                code="INVALID_AGENT_WORKFLOW",
                message=str(exc),
                status_code=400,
            ) from exc
        return success({"agent_id": resolved_agent_id, "workflow_id": workflow.workflow_id})

    @router.post("/agent/{agent_id}/request")
    async def submit_request(
        request: Request, agent_id: str, body: SubmitRequest
    ) -> dict[str, Any]:
        client = get_client(request, agent_id)
        try:
            structured_output = serialize_structured_output(body.structured_output)
        except (TypeError, ValueError) as exc:
            raise APIError(
                code="INVALID_STRUCTURED_OUTPUT",
                message=str(exc),
                status_code=400,
            ) from exc
        request_id = await client.post_request(
            require_message(body.message),
            session_id=require_session_id(body.session_id),
            structured_output=structured_output,
        )
        return success({"request_id": request_id})

    @router.get("/agent/{agent_id}/request/{request_id}/events")
    async def stream_request_events(
        request: Request,
        agent_id: str,
        request_id: str,
    ) -> StreamingResponse:
        client = get_client(request, agent_id)
        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            raise APIError(
                code="INVALID_REQUEST",
                message="request_id is required",
                status_code=400,
            )

        async def _generate() -> AsyncIterator[str]:
            try:
                async for event in client.stream_response(normalized_request_id):
                    event_name = str(event.get("event") or "message")
                    payload = event.get("data")
                    yield build_runtime_event_sse_payload(event_name, payload)
                    if event_name in {"request.completed", "request.error"}:
                        break
            except AgentClientError as exc:
                yield build_runtime_event_sse_payload(
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
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @router.get("/agent/{agent_id}/sessions")
    async def list_sessions(request: Request, agent_id: str) -> dict[str, Any]:
        agent = resolve_agent(request, agent_id)
        return success({"sessions": await agent.list_sessions()})

    @router.get("/agent/{agent_id}/sessions/{session_id}")
    async def get_runtime_session(
        request: Request, agent_id: str, session_id: str
    ) -> dict[str, Any]:
        agent = resolve_agent(request, agent_id)
        return success(
            await agent.get_session_info(normalize_optional_text(session_id))
        )

    @router.get("/agent/{agent_id}/sessions/{session_id}/history")
    async def get_history(
        request: Request,
        agent_id: str,
        session_id: str,
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        agent = resolve_agent(request, agent_id)
        if not session_id.strip():
            raise APIError(
                code="INVALID_REQUEST",
                message="session_id is required",
                status_code=400,
            )
        return success(
            {"turns": await agent.get_history_turns(session_id, limit=limit)}
        )

    @router.get("/agent/{agent_id}/sessions/{session_id}/signals")
    async def get_session_signals(
        request: Request,
        agent_id: str,
        session_id: str,
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        agent = resolve_agent(request, agent_id)
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise APIError(
                code="INVALID_REQUEST",
                message="session_id is required",
                status_code=400,
            )
        return success(
            {
                "agent_id": agent.app_id,
                "session_id": normalized_session_id,
                "definitions": agent.get_signal_definitions(),
                "turns": await agent.get_session_signals(
                    normalized_session_id, limit=limit
                ),
            }
        )

    @router.post("/agent/{agent_id}/sessions/{session_id}/compact")
    async def compact_session(
        request: Request,
        agent_id: str,
        session_id: str,
        body: CompactSessionRequest,
    ) -> dict[str, Any]:
        agent = resolve_agent(request, agent_id)
        if not session_id.strip():
            raise APIError(
                code="INVALID_REQUEST",
                message="session_id is required",
                status_code=400,
            )
        summary_text, turn_id = await agent.compact_session(
            session_id=session_id,
            reason=body.reason,
            session_total_tokens_reset=body.session_total_tokens_reset,
        )
        return success({"summary_text": summary_text, "turn_id": turn_id})

    @router.get("/agent/{agent_id}/session/{session_id}/trace/{trace_id}/reasoning")
    async def get_reasoning_trace(
        request: Request,
        agent_id: str,
        session_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        state = state_from_request(request)
        resolved_agent_id = require_agent_id(agent_id)
        resolved_session_id = require_session_id(session_id)
        resolved_trace_id = require_trace_id(trace_id)
        try:
            agent = state.host.get_agent(resolved_agent_id)
        except ValueError as exc:
            raise APIError(
                code="AGENT_NOT_FOUND", message=str(exc), status_code=404
            ) from exc

        events = await agent.runtime_store.list_events(
            app_id=resolved_agent_id,
            session_id=resolved_session_id,
            trace_id=resolved_trace_id,
        )
        trace_payload = build_reasoning_trace(events)
        return success(
            {
                "source": telemetry_event_source(),
                "agent_id": resolved_agent_id,
                "session_id": resolved_session_id,
                "trace_id": resolved_trace_id,
                **trace_payload,
            }
        )

    return router
