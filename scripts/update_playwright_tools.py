"""Write src/mash/browser/playwright_tools.json from the pinned Playwright MCP.

Runs ``npx @playwright/mcp@<PLAYWRIGHT_MCP_VERSION> --caps=vision`` over stdio,
lists its tools, and keeps the ones Mash relays (``mash.browser.catalog``).
Needs Node. Run after changing the pinned version:

    uv run python scripts/update_playwright_tools.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mash.browser.catalog import PLAYWRIGHT_MCP_VERSION, RELAYED_TOOLS

OUT = Path(__file__).resolve().parents[1] / "src" / "mash" / "browser" / "playwright_tools.json"


async def main() -> None:
    params = StdioServerParameters(
        command="npx",
        args=["-y", f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}", "--caps=vision", "--headless"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
    by_name = {tool.name: tool for tool in listed.tools}
    missing = [name for name in RELAYED_TOOLS if name not in by_name]
    if missing:
        raise SystemExit(f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION} has no {', '.join(missing)}")
    tools = [
        {
            "name": name,
            "description": by_name[name].description or "",
            "inputSchema": by_name[name].inputSchema,
        }
        for name in RELAYED_TOOLS
    ]
    OUT.write_text(json.dumps({"version": PLAYWRIGHT_MCP_VERSION, "tools": tools}, indent=2) + "\n")
    print(f"wrote {len(tools)} tools to {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
