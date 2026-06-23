"""Pool-level introspection routes (tools and skills)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from .common import state_from_request, success


def build_pool_router() -> APIRouter:
    router = APIRouter()

    @router.get("/tools")
    async def list_tools(request: Request) -> dict[str, Any]:
        state = state_from_request(request)
        return success({"tools": state.pool.describe_tools()})

    @router.get("/skills")
    async def list_skills(request: Request) -> dict[str, Any]:
        state = state_from_request(request)
        return success({"skills": state.pool.describe_skills()})

    return router
