"""GitHub-focused MashPy CLI application."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

from mash import Mash
from mash.commands import CommandBus
from mashnet import MCPClientError

GITHUB_PAT_ENV = "GITHUB_MCP_PAT"
GITHUB_SERVER_URL = "https://api.githubcopilot.com/mcp/"

# Load environment variables from a local .env file if present.
load_dotenv()


def _build_server_configs() -> List[Dict[str, Any]]:
    """Return server configuration for the GitHub MCP endpoint."""

    token = os.environ.get(GITHUB_PAT_ENV, "").strip()
    if not token:
        raise RuntimeError(
            f"{GITHUB_PAT_ENV} is not set. Add it to your .env file to use the GitHub MCP server."
        )
    return [
        {
            "name": "GitHub",
            "url": GITHUB_SERVER_URL,
            "description": "Official GitHub MCP server hosted by GitHub Copilot.",
            "type": "http",
            "headers": {
                "Authorization": f"Bearer {token}",
            },
        }
    ]


class PlogCLI(Mash):
    """MashPy CLI configured for the GitHub MCP server."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("log_path", Path(__file__).resolve().with_name("plog.log"))
        super().__init__("MashPy GitHub", servers=_build_server_configs(), **kwargs)

    def register_commands(self, command_bus: CommandBus) -> None:
        """Register GitHub-specific commands."""

        # Base commands already cover list/execute functionality.
        del command_bus


def main() -> int:
    """Entry point for launching the MashPy GitHub CLI."""

    try:
        PlogCLI().run()
        return 0
    except MCPClientError as exc:
        print(f"MCP error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
