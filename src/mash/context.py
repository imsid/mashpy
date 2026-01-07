"""Shared session state definitions for MCP CLI applications."""

from __future__ import annotations

from dataclasses import dataclass

from mashnet.host import Host

from .logging import Logger
from .memory import Memory
from .render import Renderer


@dataclass
class CLIContext:
    """Context object passed to command handlers."""

    app_name: str
    host: Host
    memory: Memory
    renderer: Renderer
    logger: Logger
