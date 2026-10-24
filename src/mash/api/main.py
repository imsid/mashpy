"""Host command helpers for the unified Mash CLI."""

from __future__ import annotations

import importlib
import os
from typing import Any, Sequence

import uvicorn

from mash.core.database import resolve_database_url
from mash.runtime import Pool, HostBuilder
from mash.runtime.agent_process import (
    build_agent_server,
    resolve_agent_token,
    resolve_host_api_url,
    resolve_sandbox_enabled,
    stop_with_parent,
)

from .app import run_host
from .config import MashHostConfig
from .types import MashHostApp


def _load_target(app_ref: str) -> Any:
    module_name, sep, attr_name = app_ref.partition(":")
    if not sep or not module_name.strip() or not attr_name.strip():
        raise ValueError("--host-app must be in 'module:attribute' format")

    module = importlib.import_module(module_name)
    if not hasattr(module, attr_name):
        raise ValueError(f"module '{module_name}' has no attribute '{attr_name}'")
    return getattr(module, attr_name)


def _resolve_host(app_ref: str) -> Pool:
    target = _load_target(app_ref)
    resolved = target() if callable(target) else target

    if isinstance(resolved, MashHostApp):
        resolved = resolved.factory()
    if isinstance(resolved, HostBuilder):
        return resolved.build()
    if isinstance(resolved, Pool):
        return resolved
    raise ValueError("host target must resolve to Pool, HostBuilder, or MashHostApp")


def add_serve_parser(subparsers) -> None:
    parser = subparsers.add_parser("serve", help="Run the Mash host API server")
    parser.set_defaults(handler=_run_serve_command)
    parser.add_argument(
        "--host-app",
        default=os.environ.get("MASH_HOST_APP"),
        help="Host application reference in module:attribute format.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MASH_API_HOST", "127.0.0.1"),
        help="API bind host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MASH_API_PORT") or os.environ.get("PORT") or "8000"),
        help="API bind port",
    )
    parser.add_argument(
        "--database-url",
        dest="runtime_database_url",
        default=resolve_database_url(),
        help="Required shared Postgres database URL for Mash runtime, memory, and API tables.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Optional API key (or set MASH_API_KEY)",
    )
    parser.add_argument(
        "--cors-origin",
        action="append",
        default=None,
        help="Allowed CORS origin; repeat for multiple values.",
    )


def _run_serve_command(args) -> int:
    if not args.host_app:
        raise SystemExit("host app is required. Set --host-app or MASH_HOST_APP.")

    api_key = args.api_key
    if api_key is None:
        api_key = os.environ.get("MASH_API_KEY")

    config = MashHostConfig(
        bind_host=args.host,
        bind_port=args.port,
        runtime_database_url=args.runtime_database_url,
        api_key=api_key,
        host_app=args.host_app,
        cors_allow_origins=(
            args.cors_origin
            if args.cors_origin is not None
            else MashHostConfig().cors_allow_origins
        ),
    )
    pool = _resolve_host(args.host_app)
    run_host(pool, config=config)
    return 0


def add_agent_serve_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "serve", help="Serve one agent of a pool (the host runs one per agent)"
    )
    parser.set_defaults(handler=_run_agent_serve_command)
    parser.add_argument(
        "--host-app",
        default=os.environ.get("MASH_HOST_APP"),
        help="Host application reference in module:attribute format.",
    )
    parser.add_argument("--agent", required=True, help="Agent id to serve")
    parser.add_argument(
        "--host",
        default=os.environ.get("MASH_AGENT_BIND_HOST", "127.0.0.1"),
        help="Agent server bind host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MASH_AGENT_PORT") or "9000"),
        help="Agent server bind port",
    )
    parser.add_argument(
        "--database-url",
        dest="runtime_database_url",
        default=resolve_database_url(),
        help="Shared Postgres database URL.",
    )


def _run_agent_serve_command(args) -> int:
    if not args.host_app:
        raise SystemExit("host app is required. Set --host-app or MASH_HOST_APP.")
    token = resolve_agent_token()
    if token is None:
        raise SystemExit("agent token is required. Set MASH_AGENT_TOKEN.")
    host_api_url = resolve_host_api_url()
    if host_api_url is None:
        raise SystemExit("host API URL is required. Set MASH_HOST_API_URL.")

    pool = _resolve_host(args.host_app)
    try:
        server = build_agent_server(
            pool,
            args.agent,
            token=token,
            host_api_url=host_api_url,
            runtime_database_url=args.runtime_database_url,
            sandbox_bash=resolve_sandbox_enabled(),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    stop_with_parent()
    uvicorn.run(server.app, host=args.host, port=args.port)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    from mash.cli.main import main as cli_main

    return cli_main(["host", "serve", *(argv or ())])
