"""Shared MCP client + host helpers."""

from .host import Host
from .client import MCPHTTPClient, MCPClientError

__all__ = ["Host", "MCPHTTPClient", "MCPClientError"]
