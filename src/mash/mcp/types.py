"""Typed MCP configuration models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class MCPServerConfig:
    """Typed config for registering an MCP server with a Mash app."""

    name: str
    url: str
    description: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    allowed_tools: Optional[list[str]] = None
