"""``mash sandbox``: serve and manage per-user sandboxes."""

from __future__ import annotations

from mash.sandbox.daemon import (
    MASH_SANDBOX_TOKEN_ENV,
    SANDBOX_PORT,
    WORKSPACES_ROOT,
    SandboxDaemon,
    resolve_sandbox_token,
)


def add_sandbox_parser(subparsers) -> None:
    sandbox = subparsers.add_parser("sandbox", help="Serve and manage per-user sandboxes")
    sandbox_subparsers = sandbox.add_subparsers(dest="sandbox_command")
    sandbox.set_defaults(handler=lambda _: (sandbox.print_help(), 0)[1])

    serve = sandbox_subparsers.add_parser(
        "serve", help="Run the sandbox daemon (inside a sandbox)"
    )
    serve.add_argument("--host", default="0.0.0.0", help="Bind host")
    serve.add_argument("--port", type=int, default=SANDBOX_PORT, help="Bind port")
    serve.add_argument(
        "--workspaces",
        default=WORKSPACES_ROOT,
        help="Directory holding one folder per agent",
    )
    serve.set_defaults(handler=_run_serve)


def _run_serve(args) -> int:
    import uvicorn

    token = resolve_sandbox_token()
    if token is None:
        raise SystemExit(f"sandbox token is required. Set {MASH_SANDBOX_TOKEN_ENV}.")
    daemon = SandboxDaemon(token=token, workspaces_root=args.workspaces)
    uvicorn.run(daemon.app, host=args.host, port=args.port)
    return 0


__all__ = ["add_sandbox_parser"]
