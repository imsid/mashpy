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
