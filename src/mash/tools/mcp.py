"""MCP tool adapter for converting MCP tools to Tool protocol."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from .base import ToolResult


class MCPToolAdapter:
    """Adapter to convert MCP tools to the Tool protocol."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        executor: Callable[[Dict[str, Any]], Awaitable[tuple[str, bool]]],
    ) -> None:
        """Initialize MCP tool adapter.

        Args:
            name: Tool name.
            description: Tool description.
            parameters: JSON schema for parameters.
            executor: Async function returning ``(text, is_error)``.
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
            text, is_error = await self._executor(args)
        except Exception as e:
            return ToolResult.error(f"Error executing MCP tool: {str(e)}")
        if is_error:
            return ToolResult.error(text)
        return ToolResult.success(text)

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
        executor: Callable[[Dict[str, Any]], Awaitable[tuple[str, bool]]],
    ) -> MCPToolAdapter:
        """Create an adapter from an MCP tool definition.

        Args:
            mcp_tool: MCP tool definition dictionary.
            executor: Async function returning ``(text, is_error)``.

        Returns:
            MCPToolAdapter instance.
        """
        name = mcp_tool.get('name', 'unknown')
        description = mcp_tool.get('description', '')
        parameters = mcp_tool.get('inputSchema', {})

        return cls(
            name=name,
            description=description,
            parameters=parameters,
            executor=executor,
        )
