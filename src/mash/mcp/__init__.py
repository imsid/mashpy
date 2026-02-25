"""MCP (Model Context Protocol) integration for Mash."""

from .client import MCPClientError, MCPHTTPClient
from .host import Host
from .manager import MCPManager
from .server import MCPServer
from .types import MCPServerConfig

__all__ = [
    "Host",
    "MCPHTTPClient",
    "MCPClientError",
    "MCPServer",
    "MCPManager",
    "MCPServerConfig",
]
