"""Reusable CLI framework for MCP-based applications."""

from .mashd.agent import AgentRuntime
from .mashd.models import AgentConfig
from .base import Connection, Mash
from .commands import Command, CommandBus
from .context import CLIContext
from .logging import AgentTraceEvent, CommandEvent, DebugEvent, EventLogger, LogEvent
from .memory import Memory, SqliteMemory
from .render import Renderer, RichRenderer
from .repl import Repl
from .router import CommandRouter
from .mashd.tools import ToolRegistry, ToolSpec
from .mashd.telemetry import TelemetryCollector, TokenUsage

__all__ = [
    "Mash",
    "Connection",
    "AgentConfig",
    "AgentRuntime",
    "Command",
    "CommandBus",
    "EventLogger",
    "Memory",
    "SqliteMemory",
    "RichRenderer",
    "Renderer",
    "Repl",
    "CLIContext",
    "CommandRouter",
    "ToolRegistry",
    "ToolSpec",
    "LogEvent",
    "CommandEvent",
    "DebugEvent",
    "AgentTraceEvent",
    "TelemetryCollector",
    "TokenUsage",
]
