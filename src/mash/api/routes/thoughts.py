"""Authenticated Admin thoughts feed and bundled expression assets."""

from importlib.resources import files

from fastapi import APIRouter, Request
from fastapi.responses import Response

from mash.mushy.catalog import EXPRESSIONS

from .common import APIError, normalize_optional_text, state_from_request, success


def build_thoughts_router() -> APIRouter:
    router = APIRouter()

    @router.get('/telemetry/thoughts')
    async def thoughts(request: Request, agent_id: str | None = None, trace_id: str | None = None):
        pool = state_from_request(request).pool
        agent_id = normalize_optional_text(agent_id)
        trace_id = normalize_optional_text(trace_id)
        if agent_id is not None:
            try:
                pool.get_agent(agent_id)
            except ValueError as exc:
                raise APIError(code='AGENT_NOT_FOUND', message=str(exc), status_code=404) from exc
        store = pool.get_runtime_store()
        traces = await store.list_thought_traces(agent_id, limit=50, trace_id=trace_id) if store is not None else []
        return success({'traces': traces, 'agent_id': agent_id, 'trace_id': trace_id, 'limit': 50})

    @router.get('/telemetry/thoughts/expressions/{asset}')
    async def expression(asset: str):
        # Resolve exclusively through the catalog, never through a URL path.
        allowed = {
            name
            for definition in EXPRESSIONS.values()
            for name in (definition.asset, definition.asset.removesuffix('.gif') + '.png')
        }
        if asset not in allowed:
            raise APIError(code='EXPRESSION_NOT_FOUND', message='Expression not found.', status_code=404)
        content = files('mash.mushy').joinpath('assets', asset).read_bytes()
        return Response(content, media_type='image/png' if asset.endswith('.png') else 'image/gif', headers={
            'Cache-Control': 'private, max-age=3600',
            'X-Content-Type-Options': 'nosniff',
        })

    return router
