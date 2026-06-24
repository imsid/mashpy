"""Base tool protocol and result types."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Protocol


@dataclass
class ToolResult:
    """Result from executing a tool."""

    content: str
    is_error: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    structured_output: Dict[str, Any] | None = None

    @classmethod
    def success(cls, content: str, **metadata: Any) -> ToolResult:
        """Create a success result."""
        return cls(content=content, is_error=False, metadata=metadata)

    @classmethod
    def error(cls, message: str, **metadata: Any) -> ToolResult:
        """Create an error result."""
        return cls(content=message, is_error=True, metadata=metadata)


class Tool(Protocol):
    """Protocol that all tools must implement."""

    name: str
    description: str
    parameters: Dict[str, Any]  # JSON schema
    requires_approval: bool
    # Whether this tool is safe to run concurrently with other tool calls in
    # the same turn. Defaults to True (opt-out): tools with side effects that
    # must not interleave should set this False. Tools that omit the attribute
    # are treated as parallel-safe.
    parallel_safe: bool

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        """Execute the tool with the given arguments.

        Args:
            args: Tool arguments as a dictionary.

        Returns:
            ToolResult containing the execution result.
        """
        raise NotImplementedError

    def to_llm_format(self) -> Dict[str, Any]:
        """Convert tool definition to LLM API format.

        Returns:
            Tool definition in the format expected by the LLM API.
        """
        raise NotImplementedError


@dataclass
class FunctionTool:
    """Concrete tool wrapper around a callable."""

    name: str
    description: str
    parameters: Dict[str, Any]
    _executor: Callable[[Dict[str, Any]], Awaitable[ToolResult]]
    requires_approval: bool = False
    parallel_safe: bool = True

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        """Execute the tool with the given arguments."""
        return await self._executor(args)

    def to_llm_format(self) -> Dict[str, Any]:
        """Convert tool definition to LLM API format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }
