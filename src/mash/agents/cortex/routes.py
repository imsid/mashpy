"""The Cortex API: host routes for apps to inspect and change a user's Cortex.

Mounted under the host's API prefix only when the pool enables Cortex. Entries
an app creates or edits are pinned (``origin: app``): ``cortex-build`` reads
them but never changes or deletes them. The profile is written only by builds.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from mash.api.routes.common import APIError, state_from_request, success
from mash.runtime.requests import validate_user_id

from .context import CortexRuntimeContext
from .models import CortexEntry
from .spec import CORTEX_AGENT_ID
from .store import CortexStore

_TOPIC_MAX_CHARS = 200
_CONTENT_MAX_CHARS = 4_000


class CreateEntryRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=_TOPIC_MAX_CHARS)
    content: str = Field(min_length=1, max_length=_CONTENT_MAX_CHARS)


class UpdateEntryRequest(BaseModel):
    topic: Optional[str] = Field(default=None, min_length=1, max_length=_TOPIC_MAX_CHARS)
    content: Optional[str] = Field(default=None, min_length=1, max_length=_CONTENT_MAX_CHARS)


def cortex_context(pool: Any) -> CortexRuntimeContext:
    """The context ``cortex-build`` uses, so the API and builds share one store."""
    spec = pool.get_registered_agent_spec(CORTEX_AGENT_ID)
    if spec is None:
        raise RuntimeError("Cortex is not enabled for this pool")
    return spec.runtime_context


def _store(request: Request) -> CortexStore:
    return cortex_context(state_from_request(request).pool).require_store()


def _user_id(value: str) -> str:
    try:
        return validate_user_id(value)
    except ValueError as exc:
        raise APIError(code="INVALID_USER_ID", message=str(exc), status_code=400) from exc


def _not_found(what: str) -> APIError:
    return APIError(code="CORTEX_NOT_FOUND", message=f"{what} not found", status_code=404)


def _entry(entry: CortexEntry) -> dict[str, Any]:
    return asdict(entry)


def build_cortex_router() -> APIRouter:
    router = APIRouter(prefix="/cortex")

    @router.get("/users/{user_id}")
    async def get_user(request: Request, user_id: str) -> dict[str, Any]:
        info = await _store(request).get_user(_user_id(user_id))
        if info is None:
            raise _not_found("user")
        return success(asdict(info))

    @router.get("/users/{user_id}/entries")
    async def list_entries(
        request: Request,
        user_id: str,
        q: Optional[str] = None,
        origin: Optional[Literal["build", "app"]] = None,
        limit: int = Query(default=50, ge=1, le=200),
        after: Optional[str] = None,
    ) -> dict[str, Any]:
        entries = await _store(request).list_entries(
            _user_id(user_id), q=q, origin=origin, limit=limit, after=after
        )
        return success(
            {
                "entries": [_entry(entry) for entry in entries],
                "next": entries[-1].entry_id if len(entries) == limit else None,
            }
        )

    @router.post("/users/{user_id}/entries")
    async def create_entry(
        request: Request, user_id: str, body: CreateEntryRequest
    ) -> dict[str, Any]:
        entry, version = await _store(request).create_entry(
            _user_id(user_id), topic=body.topic, content=body.content
        )
        return success({"entry": _entry(entry), "version": version})

    @router.patch("/users/{user_id}/entries/{entry_id}")
    async def update_entry(
        request: Request, user_id: str, entry_id: str, body: UpdateEntryRequest
    ) -> dict[str, Any]:
        if body.topic is None and body.content is None:
            raise APIError(
                code="INVALID_REQUEST", message="topic or content is required", status_code=400
            )
        updated = await _store(request).update_entry(
            _user_id(user_id), entry_id, topic=body.topic, content=body.content
        )
        if updated is None:
            raise _not_found("entry")
        entry, version = updated
        return success({"entry": _entry(entry), "version": version})

    @router.delete("/users/{user_id}/entries/{entry_id}")
    async def delete_entry(request: Request, user_id: str, entry_id: str) -> dict[str, Any]:
        version = await _store(request).delete_entry(_user_id(user_id), entry_id)
        if version is None:
            raise _not_found("entry")
        return success({"deleted": entry_id, "version": version})

    @router.delete("/users/{user_id}")
    async def delete_user(request: Request, user_id: str) -> dict[str, Any]:
        resolved = _user_id(user_id)
        if not await _store(request).delete_user(resolved):
            raise _not_found("user")
        return success({"deleted": resolved})

    return router


__all__ = ["build_cortex_router", "cortex_context"]
