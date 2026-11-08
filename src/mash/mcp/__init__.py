"""MCP (Model Context Protocol) integration for Mash."""

from .client import MCPClientError, MCPHTTPClient
from .manager import MCPManager
from .server import MCPServer
from .types import MCPServerConfig

__all__ = [
    "MCPHTTPClient",
    "MCPClientError",
    "MCPServer",
    "MCPManager",
    "MCPServerConfig",
]
