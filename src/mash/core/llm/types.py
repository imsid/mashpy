"""Provider-agnostic LLM request/response models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import SystemPrompt


@dataclass
class LLMContentBlock:
    """Normalized content block used by the Mash runtime."""

    type: str
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert block to a plain dictionary."""
        return {"type": self.type, **self.data}

    @classmethod
    def text(cls, text: str) -> "LLMContentBlock":
        return cls(type="text", data={"text": text})

    @classmethod
    def tool_call(
        cls,
        *,
        tool_call_id: str,
        name: str,
        arguments: Dict[str, Any],
    ) -> "LLMContentBlock":
        return cls(
            type="tool_call",
            data={
                "id": tool_call_id,
                "name": name,
                "arguments": arguments,
            },
        )

    @classmethod
    def tool_result(
        cls,
        *,
        tool_call_id: str,
        content: str,
        is_error: bool = False,
    ) -> "LLMContentBlock":
        return cls(
            type="tool_result",
            data={
                "tool_call_id": tool_call_id,
                "content": content,
                "is_error": is_error,
            },
        )


@dataclass
class LLMMessage:
    """Normalized message for provider adapters."""

    role: str
    content: List[LLMContentBlock]
    tool_call_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "role": self.role,
            "content": [block.to_dict() for block in self.content],
        }
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.metadata:
            data["metadata"] = self.metadata.copy()
        return data


@dataclass
class LLMToolDefinition:
    """Normalized tool definition."""

    name: str
    description: str
    parameters_json_schema: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_debug_dict(self) -> Dict[str, Any]:
        """Convert to a debug-friendly dictionary."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters_json_schema": self.parameters_json_schema,
            **self.metadata,
        }


@dataclass
class LLMTokenUsage:
    """Normalized token usage."""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMRequest:
    """Normalized LLM request."""

    model: str
    system: SystemPrompt
    messages: List[LLMMessage]
    tools: List[LLMToolDefinition]
    max_tokens: int
    temperature: float = 1.0
    use_prompt_caching: bool = True
    streaming: bool = False
    provider_options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Normalized LLM response."""

    text: str
    tool_calls: List[Any]
    content_blocks: List[LLMContentBlock]
    stop_reason: Optional[str] = None
    usage: Optional[LLMTokenUsage] = None
    provider_response: Any = None
    provider_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMCapabilities:
    """Optional provider capabilities beyond the core contract.

    The ``native_tool_calling``/``structured_output``/``prompt_caching``/
    ``reasoning_content`` flags describe how much of the harness a model can
    support natively. They exist primarily for open-source models served over
    the Chat Completions wire (:class:`OSSCompatibleProvider`), which branches
    on them; the frontier adapters leave them at their defaults.
    """

    beta_flags: bool = False
    reasoning_controls: bool = False
    server_tools: bool = False
    streaming: bool = False
    native_tool_calling: bool = True
    structured_output: bool = False
    prompt_caching: bool = False
    reasoning_content: bool = False


def coerce_content_blocks(content: str | List[Dict[str, Any]]) -> List[LLMContentBlock]:
    """Normalize stored message content into runtime blocks."""
    if isinstance(content, str):
        return [LLMContentBlock.text(content)]

    blocks: List[LLMContentBlock] = []
    for block in content:
        if not isinstance(block, dict):
            blocks.append(LLMContentBlock(type="text", data={"text": str(block)}))
            continue

        block_type = str(block.get("type", "text"))
        payload = {k: v for k, v in block.items() if k != "type"}

        if block_type == "tool_use":
            block_type = "tool_call"
            payload = {
                "id": payload.get("id"),
                "name": payload.get("name"),
                "arguments": payload.get("input", {}),
            }
        elif block_type == "tool_result":
            payload = {
                "tool_call_id": payload.get("tool_call_id", payload.get("tool_use_id")),
                "content": payload.get("content", ""),
                "is_error": payload.get("is_error", False),
            }

        blocks.append(LLMContentBlock(type=block_type, data=payload))
    return blocks
