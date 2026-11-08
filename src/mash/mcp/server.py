"""MCP server wrapper: one connected client and its tool allowlist."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import MCPHTTPClient


class MCPServer:
    """A connected MCP server, limited to ``allowed_tools`` when given."""

    def __init__(
        self,
        name: str,
        client: MCPHTTPClient,
        allowed_tools: Optional[List[str]] = None,
    ) -> None:
        self.name = name
        self._client = client
        self.allowed_tools = set(allowed_tools) if allowed_tools else None

    async def disconnect(self) -> None:
        await self._client.aclose()

    async def list_tools(self) -> List[Dict[str, Any]]:
        tools = await self._client.list_tools()
        if self.allowed_tools is not None:
            tools = [
                tool for tool in tools if tool.get("name", "").lower() in self.allowed_tools
            ]
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if self.allowed_tools is not None and name.lower() not in self.allowed_tools:
            raise RuntimeError(f"Tool '{name}' is not allowed on server '{self.name}'")
        return await self._client.call_tool(name, arguments)
