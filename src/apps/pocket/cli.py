"""MashPy Pocket MCP application built on the reusable CLI framework."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

from mash import AgentConfig, Mash
from mash.commands import CommandBus
from mashnet import MCPClientError

SERVERS: List[Dict[str, str]] = [
    {
        "name": "Pocket",
        "url": "https://pocket-feed-mcp.onrender.com/mcp/pocket?token=aDbidFDN",
        "description": "Pocket MCP utilities and resources.",
    }
]


class PocketCLI(Mash):
    """MashPy Pocket CLI that manages MCP server connections."""

    def __init__(self, **kwargs: Any) -> None:
        load_dotenv()
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        anthropic_model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        kwargs.setdefault("log_path", Path(__file__).resolve().with_name("pocket.log"))
        super().__init__(
            "MashPy Pocket",
            servers=SERVERS,
            agent_config=AgentConfig(
                app_id="pocket",
                app_context=(
                    "PocketCLI context:\n"
                    "- Connects to the Pocket MCP server for company discovery and "
                    "concierge interactions.\n"
                    "- MCP tools:\n"
                    "  - search: find companies by natural-language query, domain, "
                    "or fuzzy name. Returns scored matches with summaries, "
                    "location, and stage metadata.\n"
                    "  - concierge: ask Pocket Concierge about a company to get "
                    "answers, share feedback, request demos, or flag feature ideas. "
                    "Provide the domain, question, and optional intent/context.\n"
                    "  - company_profile: load the full Pocket company profile for "
                    "a domain, including summary, tags, timeline, concierge "
                    "prompts, and available concierge commands."
                ),
                anthropic_api_key=anthropic_key,
                model=anthropic_model,
            ),
            **kwargs,
        )

    def register_commands(self, command_bus: CommandBus) -> None:
        """Register Pocket specific commands."""

        # No Pocket-specific commands yet; reuse the base set.
        del command_bus


def main() -> int:
    """Entry point for launching the MashPy Pocket CLI."""

    try:
        PocketCLI().run()
        return 0
    except MCPClientError as exc:
        print(f"MCP error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
