"""FastAPI composition for Mash host deployment and runtime APIs."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from mash.api.logging import PostgresAPIEventStore
from mash.api.middleware import APILoggingMiddleware
from mash.api.routes.agent import build_agent_router
from mash.api.routes.h2a import build_h2a_router
from mash.api.routes.evals import build_evals_router
from mash.api.routes.feedback import build_feedback_router
from mash.api.routes.host import build_host_router
from mash.api.routes.thoughts import build_thoughts_router
from mash.api.routes.pool import build_pool_router
from mash.api.routes.browser import (
    build_browser_api_router,
    build_browser_link_router,
    build_browser_router,
)
from mash.api.routes.sandbox import build_link_router, build_sandbox_router
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
from mash.otel.exporter import build_exporter
from mash.runtime import AgentClientError, Pool
from mash.runtime.client import AgentUnavailableError
from mash.runtime.host.host import AgentDisabledError
from mash.browser.manager import (
    DEFAULT_HANDOFF_SECONDS,
    DEFAULT_IDLE_SECONDS as DEFAULT_BROWSER_IDLE_SECONDS,
    DEFAULT_MAX_RUNNING as DEFAULT_BROWSER_MAX_RUNNING,
    MASH_BROWSER_HANDOFF_SECONDS_ENV,
    MASH_BROWSER_IDLE_SECONDS_ENV,
    MASH_BROWSER_MAX_RUNNING_ENV,
    BrowserManager,
)
from mash.browser.provider import DockerBrowserProvider
from mash.runtime.host.processes import AgentProcessProvider
from mash.sandbox.docker_engine import DockerEngine, DockerEngineError
from mash.sandbox.manager import (
    DEFAULT_IDLE_SECONDS,
    DEFAULT_MAX_RUNNING,
    MASH_SANDBOX_IDLE_SECONDS_ENV,
    MASH_SANDBOX_MAX_RUNNING_ENV,
    SandboxManager,
)
from mash.sandbox.provider import DockerSandboxProvider
from mash.workflows import (
    DuplicateWorkflowRunError,
    WorkflowInputValidationError,
    WorkflowNotFoundError,
    WorkflowResumeNotSupportedError,
    WorkflowUserRequiredError,
)

from .config import MashHostConfig
from .admin_ui import mount_admin_ui


# uvicorn configures this logger, so the host's startup choices show in its log.
logger = logging.getLogger("uvicorn.error")


async def default_sandboxes(config: MashHostConfig) -> SandboxManager | None:
    """Docker sandboxes when a Docker daemon answers, otherwise none.

    Without sandboxes the host runs and its agents get no sandbox tools.
    """
    host_secret = config.resolved_host_secret()
    if not host_secret:
        logger.warning(
            "no MASH_HOST_SECRET: running without sandboxes; agents get no sandbox tools"
        )
        return None
    engine = DockerEngine()
    provider = DockerSandboxProvider(host_secret=host_secret, engine=engine)
    try:
        await engine.ping()
        await provider.prepare()
    except (DockerEngineError, RuntimeError) as exc:
        await engine.close()
        logger.warning("running without sandboxes, agents get no sandbox tools: %s", exc)
        return None
    logger.info(
        "sandboxes: Docker, namespace '%s', image %s", provider.namespace, provider.image
    )
    return SandboxManager(
        provider,
        idle_seconds=float(
            os.environ.get(MASH_SANDBOX_IDLE_SECONDS_ENV) or DEFAULT_IDLE_SECONDS
        ),
        max_running=int(os.environ.get(MASH_SANDBOX_MAX_RUNNING_ENV) or DEFAULT_MAX_RUNNING),
    )


async def default_browsers(pool: Pool, config: MashHostConfig) -> BrowserManager | None:
    """Docker browsers when an agent lists sites and a Docker daemon answers.

    Without browsers the host runs and its agents get no browser tools.
    """
    if not pool.wants_browsers():
        return None
    host_secret = config.resolved_host_secret()
    if not host_secret:
        logger.warning("no MASH_HOST_SECRET: running without browsers; agents get no browser tools")
        return None
    engine = DockerEngine()
    provider = DockerBrowserProvider(host_secret=host_secret, engine=engine)
    try:
        await engine.ping()
        await provider.prepare()
    except (DockerEngineError, RuntimeError) as exc:
        await engine.close()
        logger.warning("running without browsers, agents get no browser tools: %s", exc)
        return None
    logger.info("browsers: Docker, namespace '%s', image %s", provider.namespace, provider.image)
    return BrowserManager(
        provider,
        host_secret=host_secret,
        idle_seconds=float(
            os.environ.get(MASH_BROWSER_IDLE_SECONDS_ENV) or DEFAULT_BROWSER_IDLE_SECONDS
        ),
        max_running=int(
            os.environ.get(MASH_BROWSER_MAX_RUNNING_ENV) or DEFAULT_BROWSER_MAX_RUNNING
        ),
        handoff_seconds=int(
            os.environ.get(MASH_BROWSER_HANDOFF_SECONDS_ENV) or DEFAULT_HANDOFF_SECONDS
        ),
    )


def default_agent_provider(pool: Pool, config: MashHostConfig) -> AgentProcessProvider:
    """One child process per agent, for a pool served without a configured provider."""
    host_secret = config.resolved_host_secret()
    if not config.host_app or not host_secret:
        raise RuntimeError(
            "agents need the host app and a host secret: pass --host-app "
            "(or MASH_HOST_APP) and set MASH_HOST_SECRET"
        )
    # Children reach the host on loopback unless it is bound to one address.
    bind = config.bind_host if config.bind_host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    return AgentProcessProvider(
        host_app=config.host_app,
        host_secret=host_secret,
        host_api_url=f"http://{bind}:{config.bind_port}",
        sandboxes=pool.get_sandboxes() is not None,
        browsers=pool.get_browsers() is not None,
    )


def create_app(pool: Pool, *, config: MashHostConfig | None = None) -> FastAPI:
    """Build a FastAPI app that exposes one hosted Mash deployment."""

    resolved_config = config or MashHostConfig()

    @asynccontextmanager
    async def _lifespan(application: FastAPI):
        pool.configure_runtime_database_url(
            resolved_config.resolved_runtime_database_url()
        )
        if pool.get_sandboxes() is None:
            sandboxes = await default_sandboxes(resolved_config)
            if sandboxes is not None:
                pool.configure_sandboxes(sandboxes)
        if pool.get_browsers() is None:
            browsers = await default_browsers(pool, resolved_config)
            if browsers is not None:
                pool.configure_browsers(browsers)
        if pool.list_registered_agents() and not pool.has_agent_provider():
            pool.configure_agent_provider(
                default_agent_provider(pool, resolved_config)
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
        await pool.recover_workflow_runs()
        otel_exporter = None
        runtime_store = pool.get_runtime_store()
        if runtime_store is not None:
            otel_exporter = build_exporter(
                runtime_store, workflow_store=pool.get_workflow_store()
            )
            if otel_exporter is not None:
                await otel_exporter.start()
                logger.info("Exporting traces and logs over OTLP")
        application.state.runtime_state = AppRuntimeState(
            pool=pool,
            api_event_store=api_event_store,
            eval_service=eval_service,
            api_key=resolved_config.resolved_api_key(),
            host_secret=resolved_config.resolved_host_secret(),
            public_url=resolved_config.resolved_public_url(),
            default_events_limit=max(1, int(resolved_config.default_events_limit)),
            default_search_limit=max(1, int(resolved_config.default_search_limit)),
            otel_exporter=otel_exporter,
        )
        try:
            yield
        finally:
            state = getattr(application.state, "runtime_state", None)
            if state is not None:
                if state.otel_exporter is not None:
                    await state.otel_exporter.stop()
                if state.eval_service is not None:
                    await state.eval_service._store.close()
                await state.api_event_store.close()
                if getattr(state.pool, "cortex", None) is not None:
                    from mash.agents.cortex.routes import cortex_context

                    store = cortex_context(state.pool).store
                    if store is not None:
                        await store.close()
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
        if isinstance(exc, AgentUnavailableError):
            return JSONResponse(
                status_code=503,
                content=error_payload("AGENT_UNAVAILABLE", str(exc)),
            )
        return JSONResponse(
            status_code=502,
            content=error_payload("RUNTIME_CLIENT_ERROR", str(exc)),
        )

    @app.exception_handler(AgentDisabledError)
    async def _agent_disabled_handler(_: Request, exc: AgentDisabledError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=error_payload("AGENT_DISABLED", str(exc), {"agent_id": exc.agent_id}),
        )

    @app.exception_handler(WorkflowNotFoundError)
    async def _workflow_not_found_handler(
        _: Request, exc: WorkflowNotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content=error_payload("WORKFLOW_NOT_FOUND", str(exc)),
        )

    @app.exception_handler(WorkflowResumeNotSupportedError)
    async def _workflow_resume_not_supported_handler(
        _: Request, exc: WorkflowResumeNotSupportedError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=error_payload(
                "WORKFLOW_RESUME_NOT_SUPPORTED",
                str(exc),
                {"run_id": exc.run_id, "status": exc.status},
            ),
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

    @app.exception_handler(WorkflowUserRequiredError)
    async def _workflow_user_required_handler(
        _: Request, exc: WorkflowUserRequiredError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_payload(
                "USER_ID_REQUIRED", str(exc), {"workflow_id": exc.workflow_id}
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
    api.include_router(build_thoughts_router())
    api.include_router(build_agent_router())
    api.include_router(build_host_router())
    api.include_router(build_pool_router())
    api.include_router(build_workflow_router())
    api.include_router(build_telemetry_router())
    api.include_router(build_feedback_router())
    api.include_router(build_evals_router())
    api.include_router(build_browser_api_router())
    if getattr(pool, "cortex", None) is not None:
        from mash.agents.cortex.routes import build_cortex_router

        api.include_router(build_cortex_router())
    app.include_router(api)
    app.include_router(build_h2a_router())
    app.include_router(build_sandbox_router())
    app.include_router(build_link_router())
    app.include_router(build_browser_router())
    app.include_router(build_browser_link_router())

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


def run_host(pool: Pool, *, config: MashHostConfig | None = None) -> None:
    """Run the Mash host API service with uvicorn."""

    resolved_config = config or MashHostConfig()
    app = create_app(pool, config=resolved_config)
    uvicorn.run(app, host=resolved_config.bind_host, port=resolved_config.bind_port)
