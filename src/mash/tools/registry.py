"""Tool registry for managing available tools."""

from __future__ import annotations

import inspect
from typing import Any, Dict, List, Optional

from .base import Tool


class ToolRegistry:
    """Registry for managing available tools."""

    def __init__(self) -> None:
        """Initialize an empty tool registry."""
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool.

        Args:
            tool: Tool to register.

        Raises:
            ValueError: If a tool with the same name is already registered.
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")

        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name.

        Args:
            name: Name of the tool to unregister.
        """
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        """Get a tool by name.

        Args:
            name: Name of the tool.

        Returns:
            Tool if found, None otherwise.
        """
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        """List all registered tool names.

        Returns:
            List of tool names.
        """
        return list(self._tools.keys())

    def to_llm_format(self) -> List[Dict[str, Any]]:
        """Convert all tools to LLM API format.

        Returns:
            List of tool definitions in LLM format.
        """
        return [tool.to_llm_format() for tool in self._tools.values()]

    async def shutdown(self) -> None:
        """Shut down registered tools that expose lifecycle hooks."""
        for tool in self._tools.values():
            close = getattr(tool, "shutdown", None)
            if not callable(close):
                close = getattr(tool, "close", None)
            if not callable(close):
                close = getattr(tool, "aclose", None)
            if not callable(close):
                continue
            result = close()
            if inspect.isawaitable(result):
                await result

    def __len__(self) -> int:
        """Get the number of registered tools."""
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools
