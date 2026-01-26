"""Structured event types for logging."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class LogEvent:
    """Base event written to the log destination.

    All events share these common fields:
    - event_type: Categorizes the event (e.g., "command.start", "agent.think")
    - app_id: Application identifier
    - session_id: Session identifier for grouping related events
    - payload: Additional event-specific data
    - ts: Unix timestamp when event was created
    """

    event_type: str
    app_id: str
    session_id: Optional[str]
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary for JSON encoding."""
        return {
            "event_type": self.event_type,
            "ts": self.ts,
            "app_id": self.app_id,
            "session_id": self.session_id,
            "event_class": type(self).__name__,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class CommandEvent(LogEvent):
    """Event emitted for command execution lifecycle.

    Tracks user commands from invocation to completion.

    Example event_types:
    - "command.start" - User invoked a command
    - "command.complete" - Command finished successfully
    - "command.error" - Command failed with error

    Additional fields:
    - command_name: Name of the command (e.g., "/help", "/switch")
    - args: Command arguments
    - duration_ms: How long command took to execute
    - error: Error message if command failed
    - trace_id: Trace ID if command is part of an agent execution
    """

    command_name: Optional[str] = None
    args: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    trace_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary."""
        payload = super().to_dict()
        payload.update(
            {
                "command_name": self.command_name,
                "args": self.args,
                "duration_ms": self.duration_ms,
                "error": self.error,
                "trace_id": self.trace_id,
            }
        )
        return payload


@dataclass(frozen=True)
class AgentTraceEvent(LogEvent):
    """Structured event emitted during agent execution.

    Tracks the agent's think-act-observe loop.

    Example event_types:
    - "agent.think.start" - Agent starting to think
    - "agent.think.complete" - Agent decided on action
    - "agent.act.start" - Agent executing tools
    - "agent.act.complete" - Tools executed
    - "agent.observe.complete" - Context updated with results
    - "agent.step.complete" - Full step completed

    Additional fields:
    - trace_id: Unique identifier for this execution trace
    - step_id: Step number in the agent loop
    - duration_ms: How long this operation took
    - action_type: Type of action taken (e.g., "tool_call", "response", "finish")
    - tool_calls: List of tools called
    - token_usage: Token counts from LLM
    """

    trace_id: Optional[str] = None
    step_id: Optional[int] = None
    duration_ms: Optional[int] = None
    action_type: Optional[str] = None
    tool_calls: Optional[List[str]] = None
    token_usage: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary."""
        payload = super().to_dict()
        payload.update(
            {
                "trace_id": self.trace_id,
                "step_id": self.step_id,
                "duration_ms": self.duration_ms,
                "action_type": self.action_type,
                "tool_calls": self.tool_calls,
                "token_usage": self.token_usage,
            }
        )
        return payload


@dataclass(frozen=True)
class MCPEvent(LogEvent):
    """Event emitted for MCP client and host operations.

    Tracks MCP server connections, tool calls, and sampling requests.

    Example event_types:
    - "mcp.host.init" - Host initialized
    - "mcp.client.connect" - Connecting to MCP server
    - "mcp.client.connected" - Successfully connected
    - "mcp.client.disconnect" - Client disconnected
    - "mcp.client.error" - Connection or execution error
    - "mcp.tool.call" - Tool invoked via MCP
    - "mcp.tool.result" - Tool execution completed
    - "mcp.sampling.request" - Sampling request from server
    - "mcp.sampling.complete" - Sampling completed

    Additional fields:
    - server_name: MCP server identifier
    - server_url: Server URL/connection string
    - tool_name: Name of tool being called
    - duration_ms: Operation duration
    - error: Error message if operation failed
    - metadata: Additional server/tool specific data
    - trace_id: Trace ID if tool call is part of an agent execution
    """

    server_name: Optional[str] = None
    server_url: Optional[str] = None
    tool_name: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    trace_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary."""
        payload = super().to_dict()
        payload.update(
            {
                "server_name": self.server_name,
                "server_url": self.server_url,
                "tool_name": self.tool_name,
                "duration_ms": self.duration_ms,
                "error": self.error,
                "metadata": self.metadata,
                "trace_id": self.trace_id,
            }
        )
        return payload


@dataclass(frozen=True)
class LLMEvent(LogEvent):
    """Event emitted for LLM provider operations.

    Tracks LLM API calls, token usage, and responses.

    Example event_types:
    - "llm.request.start" - Starting LLM API call
    - "llm.request.complete" - LLM responded successfully
    - "llm.request.error" - LLM request failed
    - "llm.stream.start" - Streaming response started
    - "llm.stream.chunk" - Received stream chunk
    - "llm.stream.complete" - Streaming completed

    Additional fields:
    - provider: LLM provider name (e.g., "anthropic", "openai")
    - model: Model identifier (e.g., "claude-3-5-sonnet-20241022")
    - duration_ms: Request duration
    - input_tokens: Tokens in the prompt
    - output_tokens: Tokens in the response
    - total_tokens: Total tokens used
    - cache_creation_input_tokens: Tokens written to cache (first request)
    - cache_read_input_tokens: Tokens read from cache (subsequent requests)
    - finish_reason: Why the response ended (e.g., "end_turn", "max_tokens")
    - error: Error message if request failed
    - metadata: Additional provider-specific data
    - trace_id: Trace ID for correlating with agent execution
    - tools: List of tool names available for this request
    - betas: List of beta feature flags used in this request
    """

    provider: Optional[str] = None
    model: Optional[str] = None
    duration_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cache_creation_input_tokens: Optional[int] = None
    cache_read_input_tokens: Optional[int] = None
    finish_reason: Optional[str] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    trace_id: Optional[str] = None
    tools: Optional[List[str]] = None
    betas: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary."""
        payload = super().to_dict()
        payload.update(
            {
                "provider": self.provider,
                "model": self.model,
                "duration_ms": self.duration_ms,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
                "cache_creation_input_tokens": self.cache_creation_input_tokens,
                "cache_read_input_tokens": self.cache_read_input_tokens,
                "finish_reason": self.finish_reason,
                "error": self.error,
                "metadata": self.metadata,
                "trace_id": self.trace_id,
                "tools": self.tools,
                "betas": self.betas,
            }
        )
        return payload


@dataclass(frozen=True)
class DebugEvent(LogEvent):
    """Free-form debug event for debugging and exception tracking.

    Use this for logging exceptions, debugging information, and other
    unstructured diagnostic data.

    Example event_types:
    - "debug.exception" - Caught exception
    - "debug.warning" - Warning condition
    - "debug.info" - Informational debug message
    - "debug.trace" - Detailed trace information

    Additional fields:
    - message: Debug message
    - exception_type: Type of exception (if applicable)
    - exception_message: Exception message
    - stack_trace: Stack trace string
    - context: Additional debug context
    """

    message: Optional[str] = None
    exception_type: Optional[str] = None
    exception_message: Optional[str] = None
    stack_trace: Optional[str] = None
    context: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize event to dictionary."""
        payload = super().to_dict()
        payload.update(
            {
                "message": self.message,
                "exception_type": self.exception_type,
                "exception_message": self.exception_message,
                "stack_trace": self.stack_trace,
                "context": self.context,
            }
        )
        return payload
