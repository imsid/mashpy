"""MCP server wrapper."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .client import MCPHTTPClient


class MCPServer:
    """Wrapper for MCP server connection."""

    def __init__(
        self,
        name: str,
        url: str,
        description: str = "",
        headers: Optional[Dict[str, str]] = None,
        allowed_tools: Optional[List[str]] = None,
    ) -> None:
        """Initialize MCP server.

        Args:
            name: Server name.
            url: Server URL.
            description: Server description.
            headers: HTTP headers for requests.
            allowed_tools: Optional list of allowed tool names (whitelist).
        """
        self.name = name
        self.url = url
        self.description = description
        self.headers = headers or {}
        self.allowed_tools = set(allowed_tools) if allowed_tools else None
        self._client: Optional["MCPHTTPClient"] = None

    def connect(self, client: "MCPHTTPClient") -> None:
        """Connect to the MCP server using a Host-managed client.

        Args:
            client: MCPHTTPClient instance from Host.
        """
        self._client = client

    def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        if self._client:
            self._client.close()
            self._client = None

    def get_server_info(self) -> Dict[str, Any]:
        """Get server information.

        Returns:
            Server info dictionary.

        Raises:
            RuntimeError: If not connected.
        """
        if not self._client:
            raise RuntimeError(f"Server '{self.name}' is not connected")
        return self._client.get_server_info()

    def list_tools(self) -> List[Dict[str, Any]]:
        """List available tools from the MCP server.

        Returns:
            List of tool definitions.

        Raises:
            RuntimeError: If not connected.
        """
        if not self._client:
            raise RuntimeError(f"Server '{self.name}' is not connected")

        tools = self._client.list_tools()

        # Filter by allowed_tools if specified
        if self.allowed_tools is not None:
            tools = [
                tool for tool in tools
                if tool.get("name", "").lower() in self.allowed_tools
            ]

        return tools

    def list_resources(self) -> List[Dict[str, Any]]:
        """List available resources from the MCP server.

        Returns:
            List of resource definitions.

        Raises:
            RuntimeError: If not connected.
        """
        if not self._client:
            raise RuntimeError(f"Server '{self.name}' is not connected")
        return self._client.list_resources()

    def list_resource_templates(self) -> List[Dict[str, Any]]:
        """List available resource templates from the MCP server.

        Returns:
            List of resource template definitions.

        Raises:
            RuntimeError: If not connected.
        """
        if not self._client:
            raise RuntimeError(f"Server '{self.name}' is not connected")
        return self._client.list_resource_templates()

    def list_prompts(self) -> List[Dict[str, Any]]:
        """List available prompts from the MCP server.

        Returns:
            List of prompt definitions.

        Raises:
            RuntimeError: If not connected.
        """
        if not self._client:
            raise RuntimeError(f"Server '{self.name}' is not connected")
        return self._client.list_prompts()

    def read_resource(self, uri: str) -> Dict[str, Any]:
        """Read a resource from the MCP server.

        Args:
            uri: Resource URI.

        Returns:
            Resource content.

        Raises:
            RuntimeError: If not connected.
        """
        if not self._client:
            raise RuntimeError(f"Server '{self.name}' is not connected")
        return self._client.read_resource(uri)

    def get_prompt(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Get a prompt from the MCP server.

        Args:
            name: Prompt name.
            arguments: Prompt arguments.

        Returns:
            Prompt content.

        Raises:
            RuntimeError: If not connected.
        """
        if not self._client:
            raise RuntimeError(f"Server '{self.name}' is not connected")
        return self._client.get_prompt(name, arguments)

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call a tool on the MCP server.

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            Tool result.

        Raises:
            RuntimeError: If not connected.
        """
        if not self._client:
            raise RuntimeError(f"Server '{self.name}' is not connected")

        # Check if tool is allowed
        if self.allowed_tools is not None and name.lower() not in self.allowed_tools:
            raise RuntimeError(f"Tool '{name}' is not allowed on server '{self.name}'")

        return self._client.call_tool(name, arguments)

    def is_connected(self) -> bool:
        """Check if connected to server.

        Returns:
            True if connected, False otherwise.
        """
        return self._client is not None
