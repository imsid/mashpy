"""Pilot CLI — the storefront for a Pilot deployment.

The deployment is a flat pool of agents; which agents work together is your
configuration. Browse the pool with `pilot browse`, compose hosts with
`pilot compose` (saved to the host config file, see `pilot.store`), and
enter one with `pilot repl --host <id>` — the REPL is scoped to that host.
"""

from __future__ import annotations

import argparse
import importlib
import os
from typing import Any, Sequence

from mash.cli.client import MashHostClient
from mash.cli.render import RichRenderer
from mash.cli.shell import MashRemoteShell, ShellTarget

from . import store
from .catalog.agents.pilot import PILOT_AGENT_ID
from .catalog.workflows.changelog import register_changelog_command
from .catalog.workflows.quiz import QUIZ_WORKFLOW_ID, register_quiz_command

PILOT_DEFAULT_API_BASE_URL = os.environ.get(
    "PILOT_API_BASE_URL",
    "http://127.0.0.1:8000",
)


def _resolve_connection(args: argparse.Namespace) -> tuple[str, str | None]:
    base_url = (
        args.api_base_url
        or os.environ.get("MASH_API_BASE_URL")
        or PILOT_DEFAULT_API_BASE_URL
    ).strip()
    api_key = args.api_key or os.environ.get("MASH_API_KEY") or None
    if not base_url:
        raise ValueError(
            "API base URL is required. Use --api-base-url or PILOT_API_BASE_URL."
        )
    return base_url, api_key


def _describe_host(client: MashHostClient, host_id: str) -> dict[str, Any]:
    """Fetch the merged view of a published host."""
    try:
        described = client.get_host(host_id)
    except Exception as exc:
        raise ValueError(
            f"host '{host_id}' is not available on this deployment. "
            f"Check `pilot hosts` or create it with `pilot compose`: {exc}"
        ) from exc
    primary = described.get("primary") or {}
    agent_id = primary.get("agent_id") if isinstance(primary, dict) else None
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ValueError(f"host '{host_id}' did not report a primary agent id")
    return described


def _split_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pilot",
        description="Pilot CLI — your self-hosted app store for agents.",
    )
    parser.add_argument("--api-base-url", default=None, help="Mash host base URL")
    parser.add_argument("--api-key", default=None, help="Bearer API key")
    parser.add_argument(
        "--agent",
        default=None,
        help="Target a single agent directly (bare-agent mode, no delegation)",
    )
    subparsers = parser.add_subparsers(dest="command")

    repl = subparsers.add_parser("repl", help="Enter the REPL of a composed host")
    repl.add_argument("--session-id", default=None, help="Remote session id")
    repl.add_argument(
        "--host",
        dest="target_host",
        default=None,
        help="Host to enter (see `pilot hosts`)",
    )

    subparsers.add_parser(
        "browse", help="Browse the agent pool and your configured hosts"
    )

    compose = subparsers.add_parser(
        "compose", help="Compose agents into a host (define-or-replace)"
    )
    compose.add_argument("host_id", help="Id for the composition")
    compose.add_argument("--primary", required=True, help="Primary agent id")
    compose.add_argument(
        "--subagents", default=None, help="Comma-separated subagent ids"
    )
    compose.add_argument(
        "--workflows", default=None, help="Comma-separated workflow ids"
    )

    subparsers.add_parser("hosts", help="List the hosts in your config file")

    serve = subparsers.add_parser(
        "serve", help="Run your own Pilot host (server install required)"
    )
    serve.add_argument(
        "--workspace-root",
        default=os.environ.get("PILOT_WORKSPACE_ROOT", "."),
        help="Local mashpy clone the Pilot agents operate on",
    )
    serve.add_argument("--host", default="127.0.0.1", help="API bind host")
    serve.add_argument("--port", type=int, default=8000, help="API bind port")
    serve.add_argument(
        "--api-key", dest="api_key", default=None, help="Optional API key to require"
    )
    return parser


def _run_serve(args: argparse.Namespace) -> int:
    # Import by name so the PyInstaller CLI binary does not bundle the
    # server stack; `serve` needs a source or pip install of mash-pilot.
    try:
        spec = importlib.import_module("pilot.spec")
    except ImportError as exc:
        raise ValueError(
            "`pilot serve` requires the full server install, which this "
            "binary does not include. Run it from a source checkout "
            "(`uv pip install -e .`) or start the store with `docker run` "
            f"or `docker compose up`. ({exc})"
        ) from exc
    return spec.serve(
        workspace_root=args.workspace_root,
        bind_host=args.host,
        bind_port=args.port,
        api_key=args.api_key,
    )


def _agent_listing_rows(agents: list[dict[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for agent in sorted(agents, key=lambda a: str(a.get("agent_id") or "")):
        metadata = agent.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        rows.append(
            [
                str(agent.get("agent_id") or ""),
                str(metadata.get("display_name") or ""),
                str(metadata.get("description") or ""),
            ]
        )
    return rows


def _configured_host_rows() -> list[list[str]]:
    rows: list[list[str]] = []
    for host_id, entry in sorted(store.load_hosts().items()):
        rows.append(
            [
                host_id,
                entry["primary"],
                ", ".join(entry["subagents"]),
                ", ".join(entry["workflows"]),
            ]
        )
    return rows


def _render_configured_hosts(renderer: RichRenderer) -> None:
    rows = _configured_host_rows()
    if rows:
        renderer.table(["Host", "Primary", "Subagents", "Workflows"], rows)
    else:
        renderer.info("(no hosts configured)")


def _workflow_rows(workflows: list[dict[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for workflow in sorted(workflows, key=lambda w: str(w.get("workflow_id") or "")):
        rendered_tasks = []
        for task in workflow.get("tasks") or []:
            if isinstance(task, dict):
                rendered_tasks.append(
                    f"{task.get('task_id') or ''} -> {task.get('agent_id') or ''}"
                )
        rows.append(
            [str(workflow.get("workflow_id") or ""), ", ".join(rendered_tasks)]
        )
    return rows


def _run_browse(client: MashHostClient, renderer: RichRenderer) -> int:
    renderer.info("Agent pool")
    renderer.table(
        ["Agent", "Listing", "Description"], _agent_listing_rows(client.list_agents())
    )
    renderer.info("Workflows (attach with `pilot compose ... --workflows <id>`)")
    workflow_rows = _workflow_rows(client.list_workflows())
    if workflow_rows:
        renderer.table(["Workflow", "Tasks"], workflow_rows)
    else:
        renderer.info("(none registered)")
    renderer.info(f"Configured hosts ({store.hosts_file_path()})")
    _render_configured_hosts(renderer)
    renderer.info(
        "Compose a host with `pilot compose <host-id> --primary <agent> "
        "--subagents a,b --workflows w`, then enter it with "
        "`pilot repl --host <host-id>`."
    )
    return 0


def _run_compose(
    client: MashHostClient, renderer: RichRenderer, args: argparse.Namespace
) -> int:
    subagents = _split_ids(args.subagents)
    workflows = _split_ids(args.workflows)
    client.define_host(
        args.host_id,
        primary=args.primary,
        subagents=subagents,
        workflows=workflows,
    )
    store.record_host(
        args.host_id, primary=args.primary, subagents=subagents, workflows=workflows
    )
    renderer.info(
        f"Composed host '{args.host_id}' (primary {args.primary}, "
        f"{len(subagents)} subagent(s)). Saved to {store.hosts_file_path()}."
    )
    renderer.info(f"Enter it with `pilot repl --host {args.host_id}`.")
    return 0


def _run_hosts(renderer: RichRenderer) -> int:
    _render_configured_hosts(renderer)
    renderer.info(
        "Enter one with `pilot repl --host <id>`; compose or replace one "
        "with `pilot compose`."
    )
    return 0


def _run_repl(
    client: MashHostClient,
    renderer: RichRenderer,
    args: argparse.Namespace,
    base_url: str,
) -> int:
    if not args.agent and not args.target_host:
        args.agent = PILOT_AGENT_ID

    host_workflows: list[str] = []
    if args.agent:
        agent_id, host_id = args.agent, None
    else:
        # Hosts are config: publish them to the deployment (idempotent
        # PUTs), then enter the requested one.
        store.publish_hosts(client, renderer)
        host_id = args.target_host
        described = _describe_host(client, host_id)
        agent_id = described["primary"]["agent_id"].strip()
        host_workflows = [str(w) for w in described.get("workflows") or []]

    target = ShellTarget(
        api_base_url=base_url,
        agent_id=agent_id,
        session_id=args.session_id or MashRemoteShell.new_session_id(),
        host_id=host_id,
    )
    # /agents and /workflow are host-scoped natively by mash >= 0.5.3
    # (default_commands reads ctx.host_id).
    shell = MashRemoteShell(client, target)
    # Pilot's commands are scoped too: /changelog registers its dynamic
    # workflow on the session's target agent, and only makes sense on the
    # `pilot` primary; /quiz exists only in hosts that attach pilot-quiz.
    if agent_id == PILOT_AGENT_ID:
        register_changelog_command(shell)
    if QUIZ_WORKFLOW_ID in host_workflows:
        register_quiz_command(shell)
    shell.run()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    renderer = RichRenderer()

    try:
        if args.command == "serve":
            return _run_serve(args)
        if args.command == "hosts":
            return _run_hosts(renderer)
        if args.command in ("repl", "browse", "compose"):
            base_url, api_key = _resolve_connection(args)
            client = MashHostClient(base_url, api_key=api_key)
            try:
                if args.command == "browse":
                    return _run_browse(client, renderer)
                if args.command == "compose":
                    return _run_compose(client, renderer, args)
                return _run_repl(client, renderer, args, base_url)
            finally:
                client.close()
    except Exception as exc:
        renderer.error(str(exc))
        return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
