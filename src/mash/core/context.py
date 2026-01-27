"""Core context and data structures for agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .config import SystemPrompt


class MessageRole(str, Enum):
    """Message role types."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ActionType(str, Enum):
    """Action types the agent can take."""

    TOOL_CALL = "tool_call"
    RESPONSE = "response"
    FINISH = "finish"


@dataclass
class Message:
    """A message in the conversation."""

    role: MessageRole
    content: str | List[Dict[str, Any]]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary format for LLM API."""
        return {
            "role": self.role.value,
            "content": self.content,
        }


@dataclass
class ToolCall:
    """A tool call request."""

    id: str
    name: str
    arguments: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "name": self.name,
            "input": self.arguments,
        }


@dataclass
class ToolResult:
    """Result from executing a tool."""

    tool_call_id: str
    content: str
    is_error: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary format."""
        return {
            "type": "tool_result",
            "tool_use_id": self.tool_call_id,
            "content": self.content,
            "is_error": self.is_error,
        }


@dataclass
class Action:
    """An action the agent decides to take."""

    type: ActionType
    tool_calls: List[ToolCall] = field(default_factory=list)
    response_text: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_tool_calls(cls, tool_calls: List[ToolCall]) -> Action:
        """Create a tool call action."""
        return cls(type=ActionType.TOOL_CALL, tool_calls=tool_calls)

    @classmethod
    def from_response(cls, text: str) -> Action:
        """Create a response action."""
        return cls(type=ActionType.RESPONSE, response_text=text)

    @classmethod
    def finish(cls) -> Action:
        """Create a finish action."""
        return cls(type=ActionType.FINISH)


@dataclass
class Context:
    """Execution context for the agent."""

    messages: List[Message] = field(default_factory=list)
    system_prompt: SystemPrompt = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    signals: Dict[str, Any] = field(default_factory=dict)
    is_complete: bool = False

    def add_message(
        self, role: MessageRole, content: str | List[Dict[str, Any]], **metadata: Any
    ) -> None:
        """Add a message to the context."""
        self.messages.append(Message(role=role, content=content, metadata=metadata))

    def add_user_message(self, content: str, **metadata: Any) -> None:
        """Add a user message."""
        self.add_message(MessageRole.USER, content, **metadata)

    def add_assistant_message(self, content: str, **metadata: Any) -> None:
        """Add an assistant message."""
        self.add_message(MessageRole.ASSISTANT, content, **metadata)

    def get_messages_for_llm(self) -> List[Dict[str, Any]]:
        """Get messages formatted for LLM API."""
        return [msg.to_dict() for msg in self.messages]

    def mark_complete(self) -> None:
        """Mark the context as complete."""
        self.is_complete = True


@dataclass
class Response:
    """Response from agent execution."""

    text: str
    context: Context
    signals: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_context(cls, context: Context) -> Response:
        """Create a response from context."""
        # Find the last assistant message
        last_assistant_msg = None
        for msg in reversed(context.messages):
            if msg.role == MessageRole.ASSISTANT:
                last_assistant_msg = msg
                break

        text = ""
        if last_assistant_msg:
            if isinstance(last_assistant_msg.content, str):
                text = last_assistant_msg.content
            else:
                text = "".join(
                    block.get("text", "")
                    for block in last_assistant_msg.content
                    if isinstance(block, dict) and block.get("type") == "text"
                ).strip()
        return cls(
            text=text,
            context=context,
            signals=context.signals.copy(),
            metadata=context.metadata.copy(),
        )
