"""MCP tool adapter for converting MCP tools to Tool protocol."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict

from .base import ToolResult


class MCPToolAdapter:
    """Adapter to convert MCP tools to the Tool protocol."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        executor: Callable[[Dict[str, Any]], str],
    ) -> None:
        """Initialize MCP tool adapter.

        Args:
            name: Tool name.
            description: Tool description.
            parameters: JSON schema for parameters.
            executor: Function to execute the tool.
        """
        self.name = name
        self.description = description
        self.parameters = parameters
        self.requires_approval = False
        self._executor = executor

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        """Execute the MCP tool.

        Args:
            args: Tool arguments.

        Returns:
            ToolResult with execution output.
        """
        try:
            result = self._executor(args)
            if inspect.isawaitable(result):
                result = await result
            return ToolResult.success(result)
        except Exception as e:
            return ToolResult.error(f"Error executing MCP tool: {str(e)}")

    def to_llm_format(self) -> Dict[str, Any]:
        """Convert tool definition to LLM API format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    @classmethod
    def from_mcp_tool(
        cls,
        mcp_tool: Dict[str, Any],
        executor: Callable[[Dict[str, Any]], str],
        prefix: str = "mcp_",
    ) -> MCPToolAdapter:
        """Create an adapter from an MCP tool definition.

        Args:
            mcp_tool: MCP tool definition dictionary.
            executor: Function to execute the tool.
            prefix: Prefix to add to tool name (default: "mcp_").

        Returns:
            MCPToolAdapter instance.
        """
        name = f"{prefix}{mcp_tool.get('name', 'unknown')}"
        description = mcp_tool.get('description', '')
        parameters = mcp_tool.get('inputSchema', {})

        return cls(
            name=name,
            description=description,
            parameters=parameters,
            executor=executor,
        )
