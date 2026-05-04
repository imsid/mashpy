"""Host command helpers for the unified Mash CLI."""

from __future__ import annotations

import importlib
import os
from typing import Any, Sequence

from mash.runtime import AgentHost, HostBuilder

from .app import run_host
from .config import MashHostConfig
from .types import MashHostApp


def _env_flag(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _load_target(app_ref: str) -> Any:
    module_name, sep, attr_name = app_ref.partition(":")
    if not sep or not module_name.strip() or not attr_name.strip():
        raise ValueError("--host-app must be in 'module:attribute' format")

    module = importlib.import_module(module_name)
    if not hasattr(module, attr_name):
        raise ValueError(f"module '{module_name}' has no attribute '{attr_name}'")
    return getattr(module, attr_name)


def _resolve_host(app_ref: str) -> AgentHost:
    target = _load_target(app_ref)
    resolved = target() if callable(target) else target

    if isinstance(resolved, MashHostApp):
        resolved = resolved.factory()
    if isinstance(resolved, HostBuilder):
        return resolved.build()
    if isinstance(resolved, AgentHost):
        return resolved
    raise ValueError("host target must resolve to AgentHost, HostBuilder, or MashHostApp")


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
        "--runtime-database-url",
        default=os.environ.get("MASH_RUNTIME_DATABASE_URL"),
        help="Required Postgres database URL for the hosted runtime request engine",
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
    parser.add_argument(
        "--disable-observability",
        action="store_true",
        help="Disable telemetry endpoints.",
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
        cors_allow_origins=(
            args.cors_origin
            if args.cors_origin is not None
            else MashHostConfig().cors_allow_origins
        ),
        enable_observability=not (args.disable_observability or _env_flag("MASH_DISABLE_OBSERVABILITY")),
    )
    host = _resolve_host(args.host_app)
    run_host(host, config=config)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    from mash.cli.main import main as cli_main

    return cli_main(["host", "serve", *(argv or ())])
