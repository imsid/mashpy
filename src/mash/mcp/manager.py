"""MCP manager for handling multiple MCP server connections."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from ..logging import MCPEvent, get_session_id, get_trace_id
from .client import MCPClientError
from .host import Host
from .server import MCPServer

if TYPE_CHECKING:
    from ..logging import EventLogger
    from .types import MCPServerConfig

LOGGER = logging.getLogger("mash.mcp.manager")


class MCPManager:
    """Manager for MCP server connections.

    ``connect_all`` connects each server and lists its tools once; the list is
    kept for the life of the manager, and building an agent reads it without
    touching the network. A server that fails to connect is left out and
    logged, and the others still connect.
    """

    def __init__(
        self,
        app_id: str,
        event_logger: Optional[EventLogger] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Initialize MCP manager.

        Args:
            event_logger: Optional event logger for logging MCP operations.
            session_id: Optional session ID for event logging.
            app_id: Optional app ID for event logging.
        """
        self._servers: Dict[str, MCPServer] = {}
        self._tools: Dict[str, List[Dict[str, Any]]] = {}
        self._unavailable: Dict[str, str] = {}
        self._host = Host()
        self._event_logger = event_logger
        self._session_id = session_id
        self._app_id = app_id

    def _current_session_id(self) -> Optional[str]:
        """Session of the in-flight request, falling back to the construction-time
        value. The manager is shared across requests, so events must follow the
        bound per-request session rather than a fixed field."""
        return get_session_id() or self._session_id

    def _emit_event(self, event: MCPEvent) -> None:
        if self._event_logger is None:
            return
        result = self._event_logger.emit(event)
        if not inspect.isawaitable(result):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(result)
        else:
            loop.create_task(result)

    async def connect_all(self, servers: Sequence["MCPServerConfig"]) -> None:
        """Connect each server not yet connected; log and skip any that fail."""
        for server in servers:
            if server.name in self._servers or server.name in self._unavailable:
                continue
            try:
                await self.add_server(
                    name=server.name,
                    url=server.url,
                    description=server.description,
                    headers=server.headers,
                    allowed_tools=server.allowed_tools,
                )
            except MCPClientError as exc:
                self._unavailable[server.name] = str(exc)
                LOGGER.warning(
                    "MCP server '%s' is unavailable; the agent runs without its "
                    "tools until it restarts: %s",
                    server.name,
                    exc,
                )

    async def add_server(
        self,
        name: str,
        url: str,
        description: str = "",
        headers: Optional[Dict[str, str]] = None,
        allowed_tools: Optional[List[str]] = None,
    ) -> MCPServer:
        """Connect an MCP server and list its tools.

        Raises:
            ValueError: If server name already exists.
            MCPClientError: If connecting or listing tools fails.
        """
        if name in self._servers:
            raise ValueError(f"Server '{name}' already exists")

        server = MCPServer(
            name=name,
            url=url,
            description=description,
            headers=headers,
            allowed_tools=allowed_tools,
        )
        connect_start = time.time()
        self._emit_event(
            MCPEvent(
                event_type="mcp.client.connect",
                app_id=self._app_id,
                session_id=self._current_session_id(),
                server_name=name,
                server_url=url,
                trace_id=get_trace_id(),
            )
        )
        try:
            client = await self._host.get_client(url, name, headers=headers)
            server.connect(client)
            tools = await server.list_tools()
        except Exception as e:
            self._emit_event(
                MCPEvent(
                    event_type="mcp.client.error",
                    app_id=self._app_id,
                    session_id=self._current_session_id(),
                    server_name=name,
                    server_url=url,
                    error=str(e),
                    duration_ms=int((time.time() - connect_start) * 1000),
                    trace_id=get_trace_id(),
                )
            )
            await server.disconnect()
            raise MCPClientError(f"Failed to connect to server '{name}': {e}") from e

        self._emit_event(
            MCPEvent(
                event_type="mcp.client.connected",
                app_id=self._app_id,
                session_id=self._current_session_id(),
                server_name=name,
                server_url=url,
                duration_ms=int((time.time() - connect_start) * 1000),
                metadata={"tool_count": len(tools)},
                trace_id=get_trace_id(),
            )
        )
        self._servers[name] = server
        self._tools[name] = tools
        return server

    async def remove_server(self, name: str) -> None:
        """Disconnect an MCP server and drop its tools."""
        if name in self._servers:
            server = self._servers.pop(name)
            self._tools.pop(name, None)
            await server.disconnect()
            self._emit_event(
                MCPEvent(
                    event_type="mcp.client.disconnect",
                    app_id=self._app_id,
                    session_id=self._current_session_id(),
                    server_name=name,
                    trace_id=get_trace_id(),
                )
            )

    def get_server(self, name: str) -> Optional[MCPServer]:
        """Get an MCP server by name.

        Args:
            name: Server name.

        Returns:
            MCPServer instance if found, None otherwise.
        """
        return self._servers.get(name)

    def list_servers(self) -> List[str]:
        """List all server names.

        Returns:
            List of server names.
        """
        return list(self._servers.keys())

    def get_all_tools(
        self, prefix: str = "mcp_", normalize_name: bool = True
    ) -> Dict[str, List[Dict[str, Any]]]:
        """The tools each connected server listed when it connected.

        Args:
            prefix: Prefix to add to tool names.
            normalize_name: Whether to normalize server name in tool name.

        Returns:
            Dictionary mapping server names to tool lists with prefixed names.
        """
        all_tools: Dict[str, List[Dict[str, Any]]] = {}
        for name, tools in self._tools.items():
            server_part = self._normalize_tool_name(name) if normalize_name else name
            prefixed_tools = []
            for tool in tools:
                prefixed_tool = tool.copy()
                tool_name = tool.get("name", "unknown")
                prefixed_tool["name"] = f"{prefix}{server_part}_{tool_name}"
                prefixed_tool["metadata"] = {
                    **(tool.get("metadata") or {}),
                    "server": name,
                    "original_name": tool_name,
                }
                prefixed_tools.append(prefixed_tool)
            all_tools[name] = prefixed_tools
        return all_tools

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Call a tool on a specific server.

        Raises:
            ValueError: If server not found.
            RuntimeError: If server not connected.
        """
        server = self.get_server(server_name)
        if not server:
            raise ValueError(f"Server '{server_name}' not found")

        return await server.call_tool(tool_name, arguments)

    async def disconnect_all(self) -> None:
        """Disconnect from all servers."""
        for server in self._servers.values():
            await server.disconnect()
        self._servers.clear()
        self._tools.clear()
        await self._host.aclose()

    def __len__(self) -> int:
        """Get number of servers."""
        return len(self._servers)

    def __contains__(self, name: str) -> bool:
        """Check if server exists."""
        return name in self._servers

    @staticmethod
    def _normalize_tool_name(name: str) -> str:
        """Normalize a tool/server name for use in identifiers.

        Args:
            name: Original name.

        Returns:
            Normalized name (lowercase, alphanumeric + underscore only).
        """
        return "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_")
