"""Policy API routes: ``PUT``, ``GET`` and ``DELETE /policies/{policy_id}``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from mash.policies import Policy, PolicyStore, validate_policy_id
from mash.sessions import PolicyInUseError

from .common import APIError, state_from_request, success


def _policy_id(value: str) -> str:
    try:
        return validate_policy_id(value)
    except ValueError as exc:
        raise APIError(code="INVALID_REQUEST", message=str(exc), status_code=400) from exc


def _store(request: Request) -> PolicyStore:
    store = state_from_request(request).pool.get_policy_store()
    if store is None:
        raise APIError(
            code="POLICY_STORE_UNAVAILABLE",
            message="policies are not available",
            status_code=503,
        )
    return store


def build_policies_router() -> APIRouter:
    router = APIRouter()

    @router.put("/policies/{policy_id}")
    async def put_policy(request: Request, policy_id: str, body: Policy) -> dict[str, Any]:
        """Create the policy, or replace the whole of an existing one."""
        record = await _store(request).put(_policy_id(policy_id), body)
        return success(record.to_dict())

    @router.get("/policies/{policy_id}")
    async def get_policy(request: Request, policy_id: str) -> dict[str, Any]:
        resolved = _policy_id(policy_id)
        record = await _store(request).get(resolved)
        if record is None:
            raise APIError(
                code="POLICY_NOT_FOUND",
                message=f"policy '{resolved}' not found",
                status_code=404,
            )
        return success(record.to_dict())

    @router.delete("/policies/{policy_id}")
    async def delete_policy(request: Request, policy_id: str) -> dict[str, Any]:
        """Delete the policy with its sandbox and browser profile.

        Refused while a session on the policy has a queued or active event.
        Sessions and their ledgers stay.
        """
        resolved = _policy_id(policy_id)
        pool = state_from_request(request).pool
        events = pool.get_event_service()
        if events is None:
            raise APIError(
                code="POLICY_STORE_UNAVAILABLE",
                message="policies are not available",
                status_code=503,
            )

        async def remove() -> None:
            sandboxes, browsers = pool.get_sandboxes(), pool.get_browsers()
            if sandboxes is not None:
                await sandboxes.delete(resolved)
            if browsers is not None:
                await browsers.delete(resolved)

        try:
            await events.store.delete_policy(resolved, remove)
        except PolicyInUseError as exc:
            raise APIError(code="POLICY_IN_USE", message=str(exc), status_code=409) from exc
        return success({"deleted": resolved})

    return router
