"""Host composition API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from mash.runtime.host.types import Host

from .common import (
    APIError,
    DefineHostRequest,
    state_from_request,
    success,
)


def _resolve_host_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise APIError(
            code="INVALID_REQUEST", message="host_id is required", status_code=400
        )
    return text


def build_host_router() -> APIRouter:
    router = APIRouter()

    @router.put("/hosts/{host_id}")
    async def define_host(
        request: Request, host_id: str, body: DefineHostRequest
    ) -> dict[str, Any]:
        state = state_from_request(request)
        resolved_host_id = _resolve_host_id(host_id)
        try:
            host = Host(
                host_id=resolved_host_id,
                agents=tuple(value.strip() for value in body.agents),
                workflows=tuple(value.strip() for value in body.workflows),
            )
            state.pool.define_host(host)
        except ValueError as exc:
            raise APIError(
                code="INVALID_HOST", message=str(exc), status_code=400
            ) from exc
        return success(state.pool.describe_host(resolved_host_id))

    @router.get("/hosts")
    async def list_hosts(request: Request) -> dict[str, Any]:
        state = state_from_request(request)
        return success({"hosts": state.pool.describe_hosts()})

    @router.get("/hosts/{host_id}")
    async def get_host(request: Request, host_id: str) -> dict[str, Any]:
        state = state_from_request(request)
        try:
            described = state.pool.describe_host(_resolve_host_id(host_id))
        except ValueError as exc:
            raise APIError(
                code="HOST_NOT_FOUND", message=str(exc), status_code=404
            ) from exc
        return success(described)

    @router.get("/hosts/{host_id}/snapshot")
    async def get_host_snapshot(request: Request, host_id: str) -> dict[str, Any]:
        """Live host composition and per-agent spec state.

        This is exactly what an eval experiment records at run start, so the
        admin UI can show what is about to be evaluated before running.
        """
        state = state_from_request(request)
        resolved_host_id = _resolve_host_id(host_id)
        try:
            host = state.pool.get_host(resolved_host_id)
        except ValueError as exc:
            raise APIError(
                code="HOST_NOT_FOUND", message=str(exc), status_code=404
            ) from exc
        return success(
            {
                "host_composition": state.pool.snapshot_for(host),
                "agent_spec_snapshot": state.pool.snapshot_host_agent_specs(
                    resolved_host_id
                ),
            }
        )

    return router
