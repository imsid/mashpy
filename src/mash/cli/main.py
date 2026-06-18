"""Unified CLI entrypoint for Mash deployments and host serving."""

from __future__ import annotations

import argparse
import os
from typing import Sequence

from mash import __version__, get_docs_url
from mash.api.main import add_serve_parser

from .client import MashHostClient
from .config import CLIConfig, load_config, save_config
from .render import RichRenderer
from .shell import MashRemoteShell, ShellTarget


def _print_help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 0


def _resolve_connection(
    args: argparse.Namespace,
) -> tuple[str, str | None, str | None, str | None]:
    saved = load_config()
    base_url = (getattr(args, "api_base_url", None) or os.environ.get("MASH_API_BASE_URL") or (saved.api_base_url if saved else "")).strip()
    api_key = getattr(args, "api_key", None) or os.environ.get("MASH_API_KEY") or (saved.api_key if saved else None)
    agent_id = getattr(args, "agent", None) or (saved.agent_id if saved else None)
    host_id = getattr(args, "host_id", None) or (saved.host_id if saved else None)
    if not base_url:
        raise ValueError("API base URL is required. Use --api-base-url or `mash connect`.")
    return base_url, api_key, agent_id, host_id


def _resolve_target(
    client: MashHostClient,
    explicit_agent: str | None,
    host_id: str | None,
) -> tuple[str, str | None]:
    """Resolve (agent_id, host_id) for a command.

    An explicit agent targets the bare agent; a host targets the host's
    primary with the host composition wired in.
    """
    if explicit_agent:
        return explicit_agent, None
    if host_id:
        described = client.get_host(host_id)
        primary = described.get("primary") or {}
        agent_id = str(primary.get("agent_id") or "").strip()
        if not agent_id:
            raise ValueError(f"host '{host_id}' has no primary agent")
        return agent_id, host_id
    health = client.health()
    deployment = health.get("deployment") or {}
    hosts = deployment.get("hosts") or []
    if len(hosts) == 1 and isinstance(hosts[0], dict):
        primary = str(hosts[0].get("primary") or "").strip()
        resolved_host_id = str(hosts[0].get("host_id") or "").strip()
        if primary and resolved_host_id:
            return primary, resolved_host_id
    agents = deployment.get("agents") or []
    if len(agents) == 1 and isinstance(agents[0], dict):
        agent_id = str(agents[0].get("agent_id") or "").strip()
        if agent_id:
            return agent_id, None
    raise ValueError(
        "could not resolve a target; specify --agent or --host"
    )


def _split_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _resolve_saved_connection(
    args: argparse.Namespace,
    renderer: RichRenderer,
) -> tuple[str, str | None] | None:
    saved = load_config()
    base_url = (
        args.api_base_url
        or os.environ.get("MASH_API_BASE_URL")
        or (saved.api_base_url if saved else "")
    ).strip()
    if not base_url:
        renderer.error("API base URL is required. Use --api-base-url or `mash connect`.")
        return None
    api_key = args.api_key or os.environ.get("MASH_API_KEY") or (saved.api_key if saved else None)
    return base_url, api_key


def _agent_rows(agents: list[dict]) -> list[list[str]]:
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


def _workflow_rows(workflows: list[dict]) -> list[list[str]]:
    rows: list[list[str]] = []
    for workflow in sorted(workflows, key=lambda w: str(w.get("workflow_id") or "")):
        rendered_tasks = []
        for task in workflow.get("tasks") or []:
            if isinstance(task, dict):
                rendered_tasks.append(
                    f"{task.get('task_id') or ''} -> {task.get('agent_id') or ''}"
                )
        rows.append([str(workflow.get("workflow_id") or ""), ", ".join(rendered_tasks)])
    return rows


def _host_rows(hosts: list[dict]) -> list[list[str]]:
    return [
        [
            str(host.get("host_id") or ""),
            str(host.get("primary") or ""),
            ", ".join(host.get("subagents") or []),
            ", ".join(host.get("workflows") or []),
        ]
        for host in hosts
    ]


def _run_browse(client: MashHostClient, renderer: RichRenderer) -> int:
    renderer.info("Agent pool")
    renderer.table(["Agent", "Name", "Description"], _agent_rows(client.list_agents()))

    renderer.info("Workflows (attach to a host with `mash compose ... --workflows <id>`)")
    workflow_rows = _workflow_rows(client.list_workflows())
    if workflow_rows:
        renderer.table(["Workflow", "Tasks"], workflow_rows)
    else:
        renderer.info("(none registered)")

    renderer.info("Hosts")
    host_rows = _host_rows(client.list_hosts())
    if host_rows:
        renderer.table(["Host", "Primary", "Subagents", "Workflows"], host_rows)
    else:
        renderer.info("(no hosts defined)")

    renderer.info(
        "Compose a host with `mash compose --host <id> --primary <agent> "
        "--subagents a,b`, then enter it with `mash repl`."
    )
    return 0


def _render_host_view(renderer: RichRenderer, described: dict) -> None:
    primary = described.get("primary") or {}
    members = ", ".join(
        item.get("agent_id", "") for item in described.get("subagents") or []
    )
    renderer.info(
        f"Host '{described.get('host_id')}': primary {primary.get('agent_id')}"
        + (f" [{members}]" if members else "")
    )


def _run_connect(args: argparse.Namespace) -> int:
    renderer = RichRenderer()
    connection = _resolve_saved_connection(args, renderer)
    if connection is None:
        return 1
    base_url, api_key = connection

    if args.host_id:
        client = MashHostClient(base_url, api_key=api_key)
        try:
            described = client.get_host(args.host_id)
        except Exception as exc:
            renderer.error(str(exc))
            return 1
        finally:
            client.close()
        _render_host_view(renderer, described)

    path = save_config(
        CLIConfig(
            api_base_url=base_url,
            api_key=api_key,
            agent_id=args.agent,
            host_id=args.host_id,
        )
    )
    print(f"Saved connection to {path}")
    return 0


def _run_compose(args: argparse.Namespace) -> int:
    renderer = RichRenderer()
    connection = _resolve_saved_connection(args, renderer)
    if connection is None:
        return 1
    base_url, api_key = connection

    client = MashHostClient(base_url, api_key=api_key)
    try:
        described = client.define_host(
            args.host_id,
            primary=args.primary,
            subagents=_split_ids(args.subagents),
            workflows=_split_ids(args.workflows),
        )
    except Exception as exc:
        renderer.error(str(exc))
        return 1
    finally:
        client.close()
    _render_host_view(renderer, described)

    # Pin the composition as the current target. The saved agent_id is
    # cleared because an explicit agent outranks the host at resolve time.
    path = save_config(
        CLIConfig(
            api_base_url=base_url,
            api_key=api_key,
            agent_id=None,
            host_id=args.host_id,
        )
    )
    print(f"Saved connection to {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mash",
        description="Mash CLI for self-hosted deployments and host serving.",
        epilog=f"Documentation: {get_docs_url()}",
    )
    parser.add_argument("--version", action="store_true", help="Show installed mash version and documentation URL.")
    subparsers = parser.add_subparsers(dest="command")

    connect = subparsers.add_parser(
        "connect",
        help="Persist a deployment connection and target",
    )
    connect.add_argument(
        "--api-base-url",
        default=None,
        help="Mash host base URL, e.g. http://127.0.0.1:8000 (falls back to saved config)",
    )
    connect.add_argument("--api-key", default=None, help="Optional bearer API key")
    connect.add_argument("--agent", default=None, help="Target a bare agent id (no composition)")
    connect.add_argument("--host", dest="host_id", default=None, help="Target an existing host id")

    compose = subparsers.add_parser(
        "compose",
        help="Define a host composition on the deployment and target it",
    )
    compose.add_argument(
        "--api-base-url",
        default=None,
        help="Mash host base URL (falls back to saved config)",
    )
    compose.add_argument("--api-key", default=None, help="Optional bearer API key")
    compose.add_argument("--host", dest="host_id", required=True, help="Host id to define or replace")
    compose.add_argument("--primary", required=True, help="Primary agent id")
    compose.add_argument(
        "--subagents",
        default=None,
        help="Comma-separated subagent ids",
    )
    compose.add_argument(
        "--workflows",
        default=None,
        help="Comma-separated workflow ids",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api-base-url", default=None, help="Mash host base URL")
    common.add_argument("--api-key", default=None, help="Bearer API key")
    common.add_argument("--agent", default=None, help="Target agent id (bare-agent targeting)")
    common.add_argument("--host", dest="host_id", default=None, help="Target host id")

    subparsers.add_parser("status", parents=[common], help="Show deployment status")
    subparsers.add_parser(
        "browse",
        parents=[common],
        help="Browse the pool: agents, workflows, and hosts",
    )
    subparsers.add_parser("agents", parents=[common], help="List deployment agents")
    subparsers.add_parser("hosts", parents=[common], help="List defined hosts")

    subparsers.add_parser("sessions", parents=[common], help="List sessions for an agent")

    history = subparsers.add_parser("history", parents=[common], help="Show session history")
    history.add_argument("--session-id", required=True, help="Remote session id")
    history.add_argument("--limit", type=int, default=None, help="Optional turn limit")

    repl = subparsers.add_parser("repl", parents=[common], help="Start an interactive remote REPL")
    repl.add_argument("--session-id", default=None, help="Remote session id")

    host = subparsers.add_parser("host", help="Host management commands")
    host.set_defaults(handler=lambda _: _print_help(host))
    host_subparsers = host.add_subparsers(dest="host_command")
    add_serve_parser(host_subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"mash {__version__}")
        print(f"Docs: {get_docs_url()}")
        return 0

    if args.command == "connect":
        return _run_connect(args)

    if args.command == "compose":
        return _run_compose(args)

    handler = getattr(args, "handler", None)
    if callable(handler):
        return int(handler(args))

    if args.command is None:
        parser.print_help()
        return 0

    renderer = RichRenderer()
    try:
        base_url, api_key, configured_agent, configured_host = _resolve_connection(args)
    except ValueError as exc:
        renderer.error(str(exc))
        return 1

    client = MashHostClient(base_url, api_key=api_key)
    try:
        if args.command == "browse":
            return _run_browse(client, renderer)

        if args.command == "status":
            health = client.health()
            deployment = health.get("deployment") or {}
            hosts = deployment.get("hosts") or []
            renderer.info(f"Deployment: {base_url}")
            renderer.info(f"Agents: {len(deployment.get('agents') or [])}")
            renderer.info(f"Hosts: {len(hosts)}")
            for host in hosts:
                if not isinstance(host, dict):
                    continue
                subagents = ", ".join(host.get("subagents") or [])
                renderer.info(
                    f"  {host.get('host_id')} -> {host.get('primary')}"
                    + (f" [{subagents}]" if subagents else "")
                )
            return 0

        if args.command == "agents":
            agents = client.list_agents()
            rows = []
            for agent in agents:
                metadata = agent.get("metadata") or {}
                rows.append(
                    [
                        str(agent.get("agent_id") or ""),
                        str(metadata.get("display_name") or ""),
                    ]
                )
            renderer.table(["Agent", "Name"], rows)
            return 0

        if args.command == "hosts":
            hosts = client.list_hosts()
            rows = [
                [
                    str(host.get("host_id") or ""),
                    str(host.get("primary") or ""),
                    ", ".join(host.get("subagents") or []),
                    ", ".join(host.get("workflows") or []),
                ]
                for host in hosts
            ]
            renderer.table(["Host", "Primary", "Subagents", "Workflows"], rows)
            return 0

        agent_id, target_host_id = _resolve_target(
            client,
            getattr(args, "agent", None) or configured_agent,
            getattr(args, "host_id", None) or configured_host,
        )

        if args.command == "sessions":
            sessions_payload = client.list_sessions(agent_id)
            rows = [
                [
                    str(session.get("session_id") or ""),
                    str(session.get("turn_count") or 0),
                    str(session.get("session_total_tokens") or 0),
                ]
                for session in sessions_payload
            ]
            renderer.table(["Session ID", "Turns", "Tokens"], rows)
            return 0

        if args.command == "history":
            turns = client.get_history(agent_id, args.session_id, limit=args.limit)
            if not turns:
                renderer.warn("No conversation history.")
                return 0
            for index, turn in enumerate(turns, 1):
                renderer.info(f"Turn {index}")
                renderer.print(f"User: {turn['user_message']}")
                renderer.print(f"Agent: {turn['agent_response']}")
            return 0

        if args.command == "repl":
            target = ShellTarget(
                api_base_url=base_url,
                agent_id=agent_id,
                session_id=args.session_id or MashRemoteShell.new_session_id(),
                host_id=target_host_id,
            )
            MashRemoteShell(client, target).run()
            return 0
    except Exception as exc:
        renderer.error(str(exc))
        return 1
    finally:
        client.close()

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
