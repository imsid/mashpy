"""MCP manager for handling multiple MCP server connections."""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from ..logging import MCPEvent, get_session_id, get_trace_id
from .client import MCPClientError, MCPHTTPClient
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
        self._servers: Dict[str, MCPServer] = {}
        self._tools: Dict[str, List[Dict[str, Any]]] = {}
        self._event_logger = event_logger
        self._session_id = session_id
        self._app_id = app_id

    def _current_session_id(self) -> Optional[str]:
        """Session of the in-flight request, falling back to the construction-time
        value. The manager is shared across requests, so events must follow the
        bound per-request session rather than a fixed field."""
        return get_session_id() or self._session_id

    async def _emit_event(self, event: MCPEvent) -> None:
        if self._event_logger is not None:
            await self._event_logger.emit(event)

    async def connect_all(self, servers: Sequence["MCPServerConfig"]) -> None:
        """Connect each server; log and skip any that fail."""
        for server in servers:
            try:
                await self.add_server(server)
            except MCPClientError as exc:
                LOGGER.warning(
                    "MCP server '%s' is unavailable; the agent runs without its "
                    "tools until it restarts: %s",
                    server.name,
                    exc,
                )

    async def add_server(self, config: "MCPServerConfig") -> MCPServer:
        """Connect an MCP server and list its tools.

        Raises:
            ValueError: If server name already exists.
            MCPClientError: If connecting or listing tools fails.
        """
        name = config.name
        if name in self._servers:
            raise ValueError(f"Server '{name}' already exists")

        client = MCPHTTPClient(
            config.url,
            client_name=name,
            default_headers=config.headers,
            elicitation_handler=_handle_elicitation_request,
        )
        server = MCPServer(name=name, client=client, allowed_tools=config.allowed_tools)
        connect_start = time.time()
        await self._emit_event(
            MCPEvent(
                event_type="mcp.client.connect",
                app_id=self._app_id,
                session_id=self._current_session_id(),
                server_name=name,
                server_url=config.url,
                trace_id=get_trace_id(),
            )
        )
        try:
            await client.connect()
            tools = await server.list_tools()
        except Exception as e:
            await client.aclose()
            await self._emit_event(
                MCPEvent(
                    event_type="mcp.client.error",
                    app_id=self._app_id,
                    session_id=self._current_session_id(),
                    server_name=name,
                    server_url=config.url,
                    error=str(e),
                    duration_ms=int((time.time() - connect_start) * 1000),
                    trace_id=get_trace_id(),
                )
            )
            raise MCPClientError(f"Failed to connect to server '{name}': {e}") from e

        await self._emit_event(
            MCPEvent(
                event_type="mcp.client.connected",
                app_id=self._app_id,
                session_id=self._current_session_id(),
                server_name=name,
                server_url=config.url,
                duration_ms=int((time.time() - connect_start) * 1000),
                metadata={"tool_count": len(tools)},
                trace_id=get_trace_id(),
            )
        )
        self._servers[name] = server
        self._tools[name] = tools
        return server

    def get_all_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        """The tools each connected server listed when it connected.

        Each tool is named ``mcp_<server>_<tool>``, with the server and the
        original name in its ``metadata``.
        """
        all_tools: Dict[str, List[Dict[str, Any]]] = {}
        for name, tools in self._tools.items():
            server_part = self._normalize_tool_name(name)
            all_tools[name] = [
                {
                    **tool,
                    "name": f"mcp_{server_part}_{tool.get('name', 'unknown')}",
                    "metadata": {
                        **(tool.get("metadata") or {}),
                        "server": name,
                        "original_name": tool.get("name", "unknown"),
                    },
                }
                for tool in tools
            ]
        return all_tools

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Call a tool on a specific server.

        Raises:
            ValueError: If server not found.
        """
        server = self._servers.get(server_name)
        if server is None:
            raise ValueError(f"Server '{server_name}' not found")
        return await server.call_tool(tool_name, arguments)

    async def disconnect_all(self) -> None:
        """Disconnect from all servers."""
        for server in self._servers.values():
            await server.disconnect()
        self._servers.clear()
        self._tools.clear()

    @staticmethod
    def _normalize_tool_name(name: str) -> str:
        """Lowercase, alphanumeric + underscore only."""
        return "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_")


def _handle_elicitation_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Prompt the operator for more input when servers request it."""
    question = (
        request.get("message")
        or request.get("prompt")
        or "Server requested additional input"
    )
    print(f"[elicitation] {question}")
    answer = input("> ")
    return {
        "elicitationId": request.get("elicitationId") or str(uuid.uuid4()),
        "response": answer,
    }
