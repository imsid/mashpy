"""Core context and data structures for agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .config import SystemPrompt
from .llm.types import LLMMessage, coerce_content_blocks


class MessageRole(str, Enum):
    """Message role types."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


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

    def to_llm_message(self) -> LLMMessage:
        """Convert the stored message to a normalized LLM message."""
        tool_call_id = self.metadata.get("tool_call_id")
        if tool_call_id is not None:
            tool_call_id = str(tool_call_id)
        return LLMMessage(
            role=self.role.value,
            content=coerce_content_blocks(self.content),
            tool_call_id=tool_call_id,
            metadata=self.metadata.copy(),
        )


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
class SkillUsed:
    """A skill used object."""

    skill_id: str
    type: str
    version: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary format."""
        return {
            "skill_id": self.skill_id,
            "type": self.type,
            "version": self.version,
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
            "tool_call_id": self.tool_call_id,
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
    def from_tool_calls(
        cls, tool_calls: List[ToolCall], metadata: Optional[Dict[str, Any]] = None
    ) -> Action:
        """Create a tool call action."""
        return cls(
            type=ActionType.TOOL_CALL,
            tool_calls=tool_calls,
            metadata=metadata or {},
        )

    @classmethod
    def from_response(
        cls, text: str, metadata: Optional[Dict[str, Any]] = None
    ) -> Action:
        """Create a response action."""
        return cls(
            type=ActionType.RESPONSE,
            response_text=text,
            metadata=metadata or {},
        )

    @classmethod
    def finish(cls, metadata: Optional[Dict[str, Any]] = None) -> Action:
        """Create a finish action."""
        return cls(type=ActionType.FINISH, metadata=metadata or {})


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

    def get_messages_for_llm(self) -> List[LLMMessage]:
        """Get messages formatted for the provider-neutral LLM contract."""
        return [msg.to_llm_message() for msg in self.messages]

    def mark_complete(self) -> None:
        """Mark the context as complete."""
        self.is_complete = True


@dataclass
class Response:
    """Response from agent execution."""

    text: str
    context: Context
    assistant_blocks: List[Dict[str, Any]] = field(default_factory=list)
    signals: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_context(cls, context: Context) -> Response:
        """Create a response from context."""
        last_assistant_msg = None
        for msg in reversed(context.messages):
            if msg.role == MessageRole.ASSISTANT:
                last_assistant_msg = msg
                break

        text = ""
        blocks: List[Dict[str, Any]] = []
        if last_assistant_msg:
            if isinstance(last_assistant_msg.content, str):
                text = last_assistant_msg.content
            else:
                blocks = [
                    b for b in last_assistant_msg.content
                    if isinstance(b, dict) and b.get("type") != "tool_call"
                ]
                text = "".join(
                    b.get("text", "")
                    for b in blocks
                    if b.get("type") == "text"
                ).strip()
        return cls(
            text,
            context,
            blocks,
            context.signals.copy(),
            context.metadata.copy(),
        )
