"""MCP manager for handling multiple MCP server connections."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .server import MCPServer


class MCPManager:
    """Manager for MCP server connections."""

    def __init__(self) -> None:
        """Initialize MCP manager."""
        self._servers: Dict[str, MCPServer] = {}

    def add_server(
        self,
        name: str,
        url: str,
        description: str = "",
        headers: Optional[Dict[str, str]] = None,
        auto_connect: bool = True,
    ) -> MCPServer:
        """Add an MCP server.

        Args:
            name: Server name (unique identifier).
            url: Server URL.
            description: Server description.
            headers: HTTP headers for requests.
            auto_connect: Whether to automatically connect.

        Returns:
            MCPServer instance.

        Raises:
            ValueError: If server name already exists.
        """
        if name in self._servers:
            raise ValueError(f"Server '{name}' already exists")

        server = MCPServer(
            name=name,
            url=url,
            description=description,
            headers=headers,
        )

        if auto_connect:
            server.connect()

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

    def get_all_tools(self, prefix: str = "mcp_") -> Dict[str, List[Dict[str, Any]]]:
        """Get all tools from all connected servers.

        Args:
            prefix: Prefix to add to tool names.

        Returns:
            Dictionary mapping server names to tool lists.
        """
        all_tools: Dict[str, List[Dict[str, Any]]] = {}

        for name, server in self._servers.items():
            if server.is_connected():
                tools = server.list_tools()
                # Add prefix to tool names
                prefixed_tools = []
                for tool in tools:
                    prefixed_tool = tool.copy()
                    prefixed_tool["name"] = f"{prefix}{name}_{tool['name']}"
                    prefixed_tools.append(prefixed_tool)
                all_tools[name] = prefixed_tools

        return all_tools

    def disconnect_all(self) -> None:
        """Disconnect from all servers."""
        for server in self._servers.values():
            server.disconnect()

    def __len__(self) -> int:
        """Get number of servers."""
        return len(self._servers)

    def __contains__(self, name: str) -> bool:
        """Check if server exists."""
        return name in self._servers
