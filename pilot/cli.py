"""Pilot-specific remote CLI extensions."""

from __future__ import annotations

import argparse
import os
from typing import Sequence

from mash.cli.client import MashHostClient
from mash.cli.config import load_config
from mash.cli.render import RichRenderer
from mash.cli.shell import MashRemoteShell, ShellTarget

from .changelog import register_changelog_command


def _resolve_connection(args: argparse.Namespace) -> tuple[str, str | None, str | None]:
    saved = load_config()
    base_url = (
        args.api_base_url
        or os.environ.get("MASH_API_BASE_URL")
        or (saved.api_base_url if saved else "")
    ).strip()
    api_key = (
        args.api_key
        or os.environ.get("MASH_API_KEY")
        or (saved.api_key if saved else None)
    )
    agent_id = args.agent or (saved.agent_id if saved else None)
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
        prog="pilot",
        description="Pilot CLI for Mash Pilot remote shells.",
    )
    parser.add_argument("--api-base-url", default=None, help="Mash host base URL")
    parser.add_argument("--api-key", default=None, help="Bearer API key")
    parser.add_argument("--agent", default=None, help="Target agent id")
    subparsers = parser.add_subparsers(dest="command")

    repl = subparsers.add_parser("repl", help="Start a Pilot remote REPL")
    repl.add_argument("--session-id", default=None, help="Remote session id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    renderer = RichRenderer()

    try:
        if args.command == "repl":
            base_url, api_key, configured_agent = _resolve_connection(args)
            client = MashHostClient(base_url, api_key=api_key)
            try:
                agent_id = _resolve_agent(client, configured_agent)
                target = ShellTarget(
                    api_base_url=base_url,
                    agent_id=agent_id,
                    session_id=args.session_id or MashRemoteShell.new_session_id(),
                )
                shell = MashRemoteShell(client, target)
                register_changelog_command(shell)
                shell.run()
                return 0
            finally:
                client.close()
    except Exception as exc:
        renderer.error(str(exc))
        return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
