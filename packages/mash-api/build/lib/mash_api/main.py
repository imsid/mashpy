"""CLI entrypoint for mash-api package."""

from __future__ import annotations

import argparse
import importlib
import os
from typing import Any, Sequence

import uvicorn

from mash.runtime import MashRuntimeDefinition

from .app import create_app
from .config import MashAPIConfig
from .types import MashAPIAppSpec


def _load_target(app_ref: str) -> Any:
    module_name, sep, attr_name = app_ref.partition(":")
    if not sep or not module_name.strip() or not attr_name.strip():
        raise ValueError("--app must be in 'module:attribute' format")

    module = importlib.import_module(module_name)
    if not hasattr(module, attr_name):
        raise ValueError(f"module '{module_name}' has no attribute '{attr_name}'")
    return getattr(module, attr_name)


def _resolve_spec(app_ref: str) -> MashAPIAppSpec:
    target = _load_target(app_ref)
    resolved = target() if callable(target) else target

    if isinstance(resolved, MashAPIAppSpec):
        return resolved
    if isinstance(resolved, MashRuntimeDefinition):
        return MashAPIAppSpec(definition=resolved)

    raise ValueError(
        "app target must resolve to MashRuntimeDefinition or MashAPIAppSpec"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run mash-api server")
    parser.add_argument(
        "--app",
        required=True,
        help="Application reference in module:attribute format.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="API bind host")
    parser.add_argument("--port", type=int, default=8000, help="API bind port")
    parser.add_argument(
        "--runtime-bind-host",
        default="127.0.0.1",
        help="Internal mash runtime bind host",
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
    parser.add_argument("--log-path", default=None, help="Observability log path")
    parser.add_argument(
        "--memory-db",
        default=None,
        help="Optional SQLite path for /telemetry/memory/search",
    )
    parser.add_argument(
        "--disable-observability",
        action="store_true",
        help="Disable telemetry endpoints.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    spec = _resolve_spec(args.app)

    api_key = args.api_key
    if api_key is None:
        api_key = os.environ.get("MASH_API_KEY")

    config = MashAPIConfig(
        bind_host=args.host,
        bind_port=args.port,
        runtime_bind_host=args.runtime_bind_host,
        api_key=api_key,
        cors_allow_origins=(
            args.cors_origin
            if args.cors_origin is not None
            else MashAPIConfig().cors_allow_origins
        ),
        enable_observability=not args.disable_observability,
        observability_log_path=args.log_path,
        observability_memory_db_path=args.memory_db,
    )

    app = create_app(spec.definition, subagents=spec.subagents, config=config)
    uvicorn.run(app, host=config.bind_host, port=config.bind_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
