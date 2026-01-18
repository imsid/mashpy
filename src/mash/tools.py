"""Tool registry and invocation helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from .context import CLIContext

ToolInvoke = Callable[[Dict[str, Any], Optional[CLIContext]], Any]


@dataclass(frozen=True)
class ToolSpec:
    """Definition for an invokable tool."""

    name: str
    description: str
    input_schema: Dict[str, Any]
    source: str
    tags: Set[str] = field(default_factory=set)
    metadata: Dict[str, Any] = field(default_factory=dict)
    invoke: ToolInvoke = field(repr=False, default=lambda _args, _ctx: None)

    def to_anthropic_tool(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ToolResult:
    """Normalized tool invocation result."""

    name: str
    content: str
    raw: Any = None
    is_error: bool = False


class ToolRegistry:
    """Registers tools and provides invocation helpers."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> None:
        self._tools[tool.name] = tool

    def list_tools(self) -> List[ToolSpec]:
        return list(self._tools.values())

    def to_anthropic_tools(self, *, enable_search: bool) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        for tool in self._tools.values():
            payload = tool.to_anthropic_tool()
            if enable_search and tool.source != "memory":
                payload["defer_loading"] = True
            tools.append(payload)
        return tools

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def invoke(
        self,
        name: str,
        args: Dict[str, Any],
        ctx: Optional[CLIContext] = None,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(name=name, content=f"Unknown tool: {name}", is_error=True)
        try:
            result = tool.invoke(args, ctx)
        except Exception as exc:
            return ToolResult(
                name=name, content=f"Tool error: {exc}", raw=exc, is_error=True
            )
        return _normalize_tool_result(name, result)


def normalize_tool_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip())
    cleaned = cleaned.strip("_")
    return cleaned or "tool"


def format_tool_payload(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2)
    except TypeError:
        return str(value)


def _normalize_tool_result(name: str, result: Any) -> ToolResult:
    if isinstance(result, ToolResult):
        return result
    if isinstance(result, str):
        return ToolResult(name=name, content=result)
    if result is None:
        return ToolResult(name=name, content="ok")
    return ToolResult(name=name, content=format_tool_payload(result), raw=result)
