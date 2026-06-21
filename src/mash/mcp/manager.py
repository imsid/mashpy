"""MCP manager for handling multiple MCP server connections."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..logging import MCPEvent, get_session_id, get_trace_id
from .client import MCPClientError
from .host import Host
from .server import MCPServer

if TYPE_CHECKING:
    from ..logging import EventLogger


class MCPManager:
    """Manager for MCP server connections."""

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

    def add_server(
        self,
        name: str,
        url: str,
        description: str = "",
        headers: Optional[Dict[str, str]] = None,
        allowed_tools: Optional[List[str]] = None,
        auto_connect: bool = True,
    ) -> MCPServer:
        """Add an MCP server.

        Args:
            name: Server name (unique identifier).
            url: Server URL.
            description: Server description.
            headers: HTTP headers for requests.
            allowed_tools: Optional list of allowed tool names.
            auto_connect: Whether to automatically connect.

        Returns:
            MCPServer instance.

        Raises:
            ValueError: If server name already exists.
            MCPClientError: If connection fails.
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

        if auto_connect:
            connect_start = time.time()

            # Log connection attempt
            if self._event_logger:
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
                client = self._host.get_client(url, name, headers=headers)
                server.connect(client)

                # Log successful connection
                if self._event_logger:
                    # Get tool count for metadata
                    tool_count = 0
                    try:
                        tools = server.list_tools()
                        tool_count = len(tools)
                    except Exception:
                        pass

                    self._emit_event(
                        MCPEvent(
                            event_type="mcp.client.connected",
                            app_id=self._app_id,
                            session_id=self._current_session_id(),
                            server_name=name,
                            server_url=url,
                            duration_ms=int((time.time() - connect_start) * 1000),
                            metadata={"tool_count": tool_count},
                            trace_id=get_trace_id(),
                        )
                    )
            except MCPClientError as e:
                # Log connection error
                if self._event_logger:
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
                raise MCPClientError(
                    f"Failed to connect to server '{name}': {e}"
                ) from e

        self._servers[name] = server
        return server

    def remove_server(self, name: str) -> None:
        """Remove an MCP server.

        Args:
            name: Server name.
        """
        if name in self._servers:
            server = self._servers[name]
            server.disconnect()
            del self._servers[name]

            # Log disconnection
            if self._event_logger:
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
        """Get all tools from all connected servers.

        Args:
            prefix: Prefix to add to tool names.
            normalize_name: Whether to normalize server name in tool name.

        Returns:
            Dictionary mapping server names to tool lists with prefixed names.
        """
        all_tools: Dict[str, List[Dict[str, Any]]] = {}

        for name, server in self._servers.items():
            if server.is_connected():
                try:
                    tools = server.list_tools()
                    # Add prefix to tool names
                    prefixed_tools = []
                    for tool in tools:
                        prefixed_tool = tool.copy()
                        server_part = (
                            self._normalize_tool_name(name) if normalize_name else name
                        )
                        tool_name = tool.get("name", "unknown")
                        prefixed_tool["name"] = f"{prefix}{server_part}_{tool_name}"
                        # Store original name in metadata
                        if "metadata" not in prefixed_tool:
                            prefixed_tool["metadata"] = {}
                        prefixed_tool["metadata"]["server"] = name
                        prefixed_tool["metadata"]["original_name"] = tool_name
                        prefixed_tools.append(prefixed_tool)
                    all_tools[name] = prefixed_tools
                except Exception as e:
                    # Log but don't fail for individual server errors

                    logging.getLogger("mash.mcp.manager").error(
                        "Failed to list tools from server '%s': %s", name, e
                    )

        return all_tools

    def get_flattened_tools(
        self, prefix: str = "mcp_", normalize_name: bool = True
    ) -> List[Dict[str, Any]]:
        """Get all tools from all servers as a flat list.

        Args:
            prefix: Prefix to add to tool names.
            normalize_name: Whether to normalize server name in tool name.

        Returns:
            Flat list of all tools with prefixed names.
        """
        all_tools = self.get_all_tools(prefix=prefix, normalize_name=normalize_name)
        flattened = []
        for tools in all_tools.values():
            flattened.extend(tools)
        return flattened

    def call_tool(
        self, server_name: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Call a tool on a specific server.

        Args:
            server_name: Name of the server.
            tool_name: Name of the tool.
            arguments: Tool arguments.

        Returns:
            Tool result.

        Raises:
            ValueError: If server not found.
            RuntimeError: If server not connected.
        """
        server = self.get_server(server_name)
        if not server:
            raise ValueError(f"Server '{server_name}' not found")

        call_start = time.time()

        # Log tool call
        if self._event_logger:
            self._emit_event(
                MCPEvent(
                    event_type="mcp.tool.call",
                    app_id=self._app_id,
                    session_id=self._current_session_id(),
                    server_name=server_name,
                    tool_name=tool_name,
                    trace_id=get_trace_id(),
                )
            )

        try:
            result = server.call_tool(tool_name, arguments)

            # Log tool result
            if self._event_logger:
                self._emit_event(
                    MCPEvent(
                        event_type="mcp.tool.result",
                        app_id=self._app_id,
                        session_id=self._current_session_id(),
                        server_name=server_name,
                        tool_name=tool_name,
                        duration_ms=int((time.time() - call_start) * 1000),
                        trace_id=get_trace_id(),
                    )
                )

            return result
        except Exception as e:
            # Log tool error
            if self._event_logger:
                self._emit_event(
                    MCPEvent(
                        event_type="mcp.tool.error",
                        app_id=self._app_id,
                        session_id=self._current_session_id(),
                        server_name=server_name,
                        tool_name=tool_name,
                        error=str(e),
                        duration_ms=int((time.time() - call_start) * 1000),
                        trace_id=get_trace_id(),
                    )
                )
            raise

    def disconnect_all(self) -> None:
        """Disconnect from all servers."""
        for server in self._servers.values():
            server.disconnect()
        self._host.close()

    def __len__(self) -> int:
        """Get number of servers."""
        return len(self._servers)

    def __contains__(self, name: str) -> bool:
        """Check if server exists."""
        return name in self._servers

    def __del__(self) -> None:
        """Clean up on deletion."""
        try:
            self.disconnect_all()
        except Exception:
            pass  # Ignore errors during cleanup

    @staticmethod
    def _normalize_tool_name(name: str) -> str:
        """Normalize a tool/server name for use in identifiers.

        Args:
            name: Original name.

        Returns:
            Normalized name (lowercase, alphanumeric + underscore only).
        """
        return "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_")
