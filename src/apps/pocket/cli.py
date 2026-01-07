"""MashPy Pocket MCP application built on the reusable CLI framework."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

from mash import Mash
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
        kwargs.setdefault("log_path", Path(__file__).resolve().with_name("pocket.log"))
        super().__init__("MashPy Pocket", servers=SERVERS, **kwargs)

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
