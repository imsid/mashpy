"""Workflow API routes."""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from mash.workflows import DuplicateWorkflowRunError, WorkflowNotFoundError

from .common import (
    APIError,
    RunWorkflowRequest,
    build_runtime_event_sse_payload,
    get_workflow_service,
    normalize_optional_text,
    parse_limit,
    success,
)


def build_workflow_router() -> APIRouter:
    router = APIRouter()

    @router.get("/workflow")
    async def list_workflows(request: Request) -> dict[str, Any]:
        workflow_service = get_workflow_service(request)
        return success({"workflows": await workflow_service.list_workflows()})

    @router.post("/workflow/{workflow_id}/run")
    async def run_workflow(
        request: Request,
        workflow_id: str,
        body: RunWorkflowRequest,
    ) -> dict[str, Any]:
        workflow_service = get_workflow_service(request)
        try:
            run = await workflow_service.run_workflow(
                workflow_id.strip(),
                dedup_key=normalize_optional_text(body.dedup_key),
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
        return success(
            {
                "run_id": run.run_id,
                "workflow_id": run.workflow_id,
                "status": run.status,
            }
        )

    @router.get("/workflow/{workflow_id}/runs")
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
        workflow_service = get_workflow_service(request)
        resolved_limit = parse_limit(limit, default=50, max_value=200)
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
        return success(
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
                        "summary": run.summary,
                    }
                    for run in runs
                ],
            }
        )

    @router.get("/workflow/{workflow_id}/runs/{run_id}")
    async def get_workflow_run(
        request: Request,
        workflow_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        workflow_service = get_workflow_service(request)
        run = await workflow_service.get_run(workflow_id.strip(), run_id.strip())
        return success(
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
                "summary": run.summary,
            }
        )

    @router.get("/workflow/{workflow_id}/runs/{run_id}/events")
    async def stream_workflow_run_events(
        request: Request,
        workflow_id: str,
        run_id: str,
    ) -> StreamingResponse:
        workflow_service = get_workflow_service(request)
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
                    yield build_runtime_event_sse_payload(event.event, event.data)
            except Exception as exc:
                yield build_runtime_event_sse_payload(
                    "workflow.error",
                    {"workflow_id": workflow_id, "run_id": run_id, "error": str(exc)},
                )

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    return router
