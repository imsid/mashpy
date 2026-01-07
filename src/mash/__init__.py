"""Reusable CLI framework for MCP-based applications."""

from .base import Connection, Mash
from .commands import Command, CommandBus
from .context import CLIContext
from .logging import JsonLogger, Logger
from .memory import Memory, SqliteMemory
from .render import PlainRenderer, Renderer
from .repl import Repl

__all__ = [
    "Mash",
    "Connection",
    "Command",
    "CommandBus",
    "JsonLogger",
    "Logger",
    "Memory",
    "SqliteMemory",
    "PlainRenderer",
    "Renderer",
    "Repl",
    "CLIContext",
]
