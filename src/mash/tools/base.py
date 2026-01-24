"""Base tool protocol and result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Protocol


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

    def execute(self, args: Dict[str, Any]) -> ToolResult:
        """Execute the tool with the given arguments.

        Args:
            args: Tool arguments as a dictionary.

        Returns:
            ToolResult containing the execution result.
        """
        ...

    def to_llm_format(self) -> Dict[str, Any]:
        """Convert tool definition to LLM API format.

        Returns:
            Tool definition in the format expected by the LLM API.
        """
        ...
