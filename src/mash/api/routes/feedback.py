"""User feedback API routes.

Feedback is unrelated to observability: both endpoints are always available.
The runtime store keeps it in a dedicated table next to the event log.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from mash.runtime.events import FeedbackRecord, FeedbackType

from .common import (
    APIError,
    normalize_optional_text,
    parse_limit,
    state_from_request,
    success,
)


class SubmitFeedbackRequest(BaseModel):
    agent_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    feedback_type: str = FeedbackType.TEXT.value
    host_id: Optional[str] = None
    session_id: Optional[str] = None
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    context: dict[str, Any] = Field(default_factory=dict)


def build_feedback_router() -> APIRouter:
    router = APIRouter()

    def _resolve_agent(state, agent_id: str):
        try:
            return state.pool.get_agent(agent_id)
        except ValueError as exc:
            raise APIError(code="AGENT_NOT_FOUND", message=str(exc), status_code=404) from exc

    @router.post("/feedback")
    async def submit_feedback(request: Request, body: SubmitFeedbackRequest) -> dict[str, Any]:
        state = state_from_request(request)
        agent_id = body.agent_id.strip()
        agent = _resolve_agent(state, agent_id)

        record = FeedbackRecord(
            app_id=agent_id,
            message=body.message.strip(),
            feedback_type=body.feedback_type.strip() or FeedbackType.TEXT.value,
            host_id=normalize_optional_text(body.host_id),
            session_id=normalize_optional_text(body.session_id),
            request_id=normalize_optional_text(body.request_id),
            trace_id=normalize_optional_text(body.trace_id),
            context=body.context or {},
        )
        stored = await agent.runtime_store.append_feedback(record)
        return success({"feedback": asdict(stored)})

    @router.get("/feedback")
    async def list_feedback(
        request: Request,
        agent_id: str,
        after: float,
        before: Optional[float] = Query(default=None),
        session_id: Optional[str] = Query(default=None),
        feedback_type: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None),
    ) -> dict[str, Any]:
        state = state_from_request(request)
        agent_id_value = agent_id.strip()
        agent = _resolve_agent(state, agent_id_value)

        resolved_limit = parse_limit(limit, default=state.default_events_limit, max_value=1000)
        records = await agent.runtime_store.list_feedback(
            agent_id_value,
            after=after,
            before=before,
            feedback_type=normalize_optional_text(feedback_type),
            session_id=normalize_optional_text(session_id),
            q=normalize_optional_text(q),
            limit=resolved_limit,
        )
        return success(
            {
                "feedback": [asdict(record) for record in records],
                "agent_id": agent_id_value,
                "after": after,
                "before": before,
                "limit": resolved_limit,
            }
        )

    return router
