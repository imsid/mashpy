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


def _resolve_connection(args: argparse.Namespace) -> tuple[str, str | None, str | None]:
    saved = load_config()
    base_url = (getattr(args, "api_base_url", None) or os.environ.get("MASH_API_BASE_URL") or (saved.api_base_url if saved else "")).strip()
    api_key = getattr(args, "api_key", None) or os.environ.get("MASH_API_KEY") or (saved.api_key if saved else None)
    agent_id = getattr(args, "agent", None) or (saved.agent_id if saved else None)
    if not base_url:
        raise ValueError("API base URL is required. Use --api-base-url or `mash connect`.")
    return base_url, api_key, agent_id


def _resolve_agent(client: MashHostClient, explicit_agent: str | None) -> str:
    if explicit_agent:
        return explicit_agent
    health = client.health()
    deployment = health.get("deployment") or {}
    agent_id = deployment.get("primary_agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ValueError("could not resolve default agent id from deployment")
    return agent_id.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mash",
        description="Mash CLI for self-hosted deployments and host serving.",
        epilog=f"Documentation: {get_docs_url()}",
    )
    parser.add_argument("--version", action="store_true", help="Show installed mash version and documentation URL.")
    subparsers = parser.add_subparsers(dest="command")

    connect = subparsers.add_parser("connect", help="Persist a default mash deployment connection")
    connect.add_argument("--api-base-url", required=True, help="Mash host base URL, e.g. http://127.0.0.1:8000")
    connect.add_argument("--api-key", default=None, help="Optional bearer API key")
    connect.add_argument("--agent", default=None, help="Optional default agent id")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api-base-url", default=None, help="Mash host base URL")
    common.add_argument("--api-key", default=None, help="Bearer API key")
    common.add_argument("--agent", default=None, help="Target agent id")

    subparsers.add_parser("status", parents=[common], help="Show deployment status")
    subparsers.add_parser("agents", parents=[common], help="List deployment agents")

    invoke = subparsers.add_parser("invoke", parents=[common], help="Invoke a remote agent")
    invoke.add_argument("message", help="Prompt to send to the remote agent")
    invoke.add_argument("--session-id", default=None, help="Remote session id")

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
        path = save_config(CLIConfig(api_base_url=args.api_base_url, api_key=args.api_key, agent_id=args.agent))
        print(f"Saved connection to {path}")
        return 0

    handler = getattr(args, "handler", None)
    if callable(handler):
        return int(handler(args))

    if args.command is None:
        parser.print_help()
        return 0

    renderer = RichRenderer()
    try:
        base_url, api_key, configured_agent = _resolve_connection(args)
    except ValueError as exc:
        renderer.error(str(exc))
        return 1

    client = MashHostClient(base_url, api_key=api_key)
    try:
        agent_id = _resolve_agent(client, getattr(args, "agent", None) or configured_agent)

        if args.command == "status":
            health = client.health()
            deployment = health.get("deployment") or {}
            renderer.info(f"Deployment: {base_url}")
            renderer.info(f"Primary agent: {deployment.get('primary_agent_id')}")
            renderer.info(f"Agents: {len(deployment.get('agents') or [])}")
            return 0

        if args.command == "agents":
            agents = client.list_agents()
            rows = [[str(agent.get("agent_id") or ""), str(agent.get("role") or "")] for agent in agents]
            renderer.table(["Agent", "Role"], rows)
            return 0

        if args.command == "invoke":
            session_id = args.session_id or MashRemoteShell.new_session_id()
            with renderer.status("Invoking remote agent..."):
                result = client.invoke(agent_id, message=args.message, session_id=session_id)
            response_payload = result.get("response")
            text = str(response_payload.get("text") or "") if isinstance(response_payload, dict) else str(result.get("text") or "")
            renderer.info(f"Agent: {agent_id}")
            renderer.info(f"Session: {result.get('session_id') or session_id}")
            if text:
                renderer.markdown(text)
            return 0

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
