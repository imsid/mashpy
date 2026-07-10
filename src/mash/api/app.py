"""FastAPI composition for Mash host deployment and runtime APIs."""

from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from mash.api.logging import PostgresAPIEventStore
from mash.api.middleware import APILoggingMiddleware
from mash.api.routes.agent import build_agent_router
from mash.api.routes.evals import build_evals_router
from mash.api.routes.feedback import build_feedback_router
from mash.api.routes.host import build_host_router
from mash.api.routes.pool import build_pool_router
from mash.api.routes.common import (
    APIError,
    AppRuntimeState,
    api_key_from_request,
    error_payload,
    state_from_request,
)
from mash.api.routes.telemetry import build_telemetry_router
from mash.api.routes.workflow import build_workflow_router
from mash.agents.masher.spec import EVAL_AGENT_ID
from mash.evals import EvalService, PostgresEvalStore
from mash.runtime import AgentClientError, AgentPool
from mash.workflows import (
    DuplicateWorkflowRunError,
    WorkflowInputValidationError,
    WorkflowNotFoundError,
)

from .config import MashHostConfig
from .admin_ui import mount_admin_ui


def create_app(pool: AgentPool, *, config: MashHostConfig | None = None) -> FastAPI:
    """Build a FastAPI app that exposes one hosted Mash deployment."""

    resolved_config = config or MashHostConfig()

    @asynccontextmanager
    async def _lifespan(application: FastAPI):
        pool.configure_runtime_database_url(
            resolved_config.resolved_runtime_database_url()
        )
        await pool.start()
        database_url = resolved_config.resolved_runtime_database_url() or ""
        api_event_store = PostgresAPIEventStore(database_url)
        await api_event_store.open()
        eval_service: EvalService | None = None
        if database_url:
            eval_store = PostgresEvalStore(database_url)
            await eval_store.open()
            eval_service = EvalService(eval_store)
            eval_agent_spec = pool.get_registered_agent_spec(EVAL_AGENT_ID)
            if eval_agent_spec is not None and hasattr(eval_agent_spec, "runtime_context"):
                eval_agent_spec.runtime_context.bind_eval_service(eval_service)
        application.state.runtime_state = AppRuntimeState(
            pool=pool,
            api_event_store=api_event_store,
            eval_service=eval_service,
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
                if state.eval_service is not None:
                    await state.eval_service._store.close()
                await state.api_event_store.close()
                await state.pool.close()
            application.state.runtime_state = None

    app = FastAPI(title="Mash Host", version="1.0.0", lifespan=_lifespan)
    mount_admin_ui(app)

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
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_payload(
                "VALIDATION_ERROR",
                "request validation failed",
                {"errors": exc.errors()},
            ),
        )

    @app.exception_handler(AgentClientError)
    async def _client_error_handler(_: Request, exc: AgentClientError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content=error_payload("RUNTIME_CLIENT_ERROR", str(exc)),
        )

    @app.exception_handler(WorkflowNotFoundError)
    async def _workflow_not_found_handler(
        _: Request, exc: WorkflowNotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=error_payload("WORKFLOW_NOT_FOUND", str(exc)),
        )

    @app.exception_handler(WorkflowInputValidationError)
    async def _workflow_input_validation_error_handler(
        _: Request, exc: WorkflowInputValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_payload(
                "WORKFLOW_INPUT_INVALID",
                str(exc),
                {"workflow_id": exc.workflow_id, "errors": exc.errors},
            ),
        )

    @app.exception_handler(DuplicateWorkflowRunError)
    async def _duplicate_workflow_run_handler(
        _: Request, exc: DuplicateWorkflowRunError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=error_payload(
                "WORKFLOW_DUPLICATE_RUN",
                str(exc),
                {"run_id": exc.existing_run.run_id},
            ),
        )

    async def _authorize(request: Request) -> None:
        state = state_from_request(request)
        expected_key = state.api_key
        if expected_key is None:
            return
        if api_key_from_request(request) != expected_key:
            raise APIError(
                code="UNAUTHORIZED",
                message="valid API key is required",
                status_code=401,
            )

    api = APIRouter(
        prefix=resolved_config.api_prefix, dependencies=[Depends(_authorize)]
    )
    api.include_router(build_agent_router())
    api.include_router(build_host_router())
    api.include_router(build_pool_router())
    api.include_router(build_workflow_router())
    api.include_router(build_telemetry_router())
    api.include_router(build_feedback_router())
    api.include_router(build_evals_router())
    app.include_router(api)

    @app.get("/")
    async def root() -> dict[str, object]:
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


def run_host(pool: AgentPool, *, config: MashHostConfig | None = None) -> None:
    """Run the Mash host API service with uvicorn."""

    resolved_config = config or MashHostConfig()
    app = create_app(pool, config=resolved_config)
    uvicorn.run(app, host=resolved_config.bind_host, port=resolved_config.bind_port)
