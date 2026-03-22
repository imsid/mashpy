"""Core agent execution loop."""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from mash.skills.registry import SkillRegistry
from mash.skills.tool import SkillTool

from ..logging import AgentTraceEvent, DebugEvent, clear_trace_id, set_trace_id
from .config import AgentConfig, SystemPrompt
from .context import (
    Action,
    ActionType,
    Context,
    MessageRole,
    Response,
    ToolCall,
    ToolResult,
)
from .llm import LLMProvider
from .llm.types import LLMRequest, LLMResponse, LLMToolDefinition

if TYPE_CHECKING:
    from ..logging import EventLogger
    from ..memory.signals import SignalCollector
    from ..tools.registry import ToolRegistry


class Agent:
    """Agent that executes the think-act-observe loop."""

    def __init__(
        self,
        llm: LLMProvider,
        tools: ToolRegistry,
        skills: SkillRegistry,
        config: AgentConfig,
    ) -> None:
        """Initialize the agent.

        Args:
            llm: LLM provider for generating responses.
            tools: Tool registry containing available tools.
            config: Agent configuration.
        """
        self.llm = llm
        self.tools = tools
        self.skills = skills
        self.config = config

        if self.config.skills_enabled and self.skills.list_skills():
            if "Skill" not in self.tools:
                self.tools.register(SkillTool(self.skills))
        self._signal_collector: Optional[SignalCollector] = None
        self._event_logger: Optional[EventLogger] = None
        self._session_id: Optional[str] = None
        self._trace_id: Optional[str] = None
        self._chain_renderer: Optional[Any] = None  # ChainOfThoughtRenderer
        self._run_token_usage: Dict[str, int] = {"input": 0, "output": 0}

    def set_signal_collector(self, collector: SignalCollector) -> None:
        """Set the signal collector for automatic signal collection."""
        self._signal_collector = collector

    def set_event_logger(self, logger: EventLogger, session_id: str) -> None:
        """Set the event logger for automatic event logging.

        Args:
            logger: Event logger instance.
            session_id: Session ID for this run.
        """
        self._event_logger = logger
        self._session_id = session_id

    def get_event_logger_session_id(self) -> Optional[str]:
        """Return the currently bound event-logger session ID."""
        return self._session_id

    def set_chain_renderer(self, renderer: Any) -> None:
        """Set the chain of thought renderer for real-time visualization.

        Args:
            renderer: Chain of thought renderer instance.
        """
        self._chain_renderer = renderer

    def run(self, context: Context) -> Response:
        """Execute the agent loop.

        Args:
            context: Execution context with messages and state.

        Returns:
            Response containing the agent's output.
        """
        # Generate trace ID for this run
        self._trace_id = str(uuid.uuid4())
        # Set trace ID in thread-local context for cross-component correlation
        if self._event_logger:
            set_trace_id(self._trace_id)

        # Reset per-run token usage accumulator
        self._run_token_usage = {"input": 0, "output": 0}

        # Set trace ID on LLM provider for event correlation
        if hasattr(self.llm, "set_trace_id"):
            self.llm.set_trace_id(self._trace_id)

        # Log agent execution start with user message
        if self._event_logger:
            # Extract user message from context
            user_message = ""
            for msg in reversed(context.messages):
                if msg.role == MessageRole.USER:
                    content = msg.content
                    if isinstance(content, str):
                        user_message = content
                    elif isinstance(content, list):
                        # Extract text from content blocks
                        text_parts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                        user_message = " ".join(text_parts)
                    break

            start_event = AgentTraceEvent(
                event_type="agent.run.start",
                app_id=self.config.app_id,
                session_id=self._session_id,
                trace_id=self._trace_id,
                payload={"user_message": user_message},
            )
            self._event_logger.emit(start_event)

        try:
            # Start chain rendering
            if self._chain_renderer:
                self._chain_renderer.start_trace(self._trace_id)

            max_steps_exhausted = True
            for step in range(self.config.max_steps):
                step_start = time.time()

                # Think: decide what to do next
                action = self.think(context)

                # Check if we're done
                if action.type == ActionType.FINISH:
                    context.mark_complete()
                    max_steps_exhausted = False
                    break

                # Act: execute the action
                results = self.act(action)

                # Observe: update context with results
                context = self.observe(context, action, results)

                # Collect signals (automatic)
                if self._signal_collector:
                    signals = self._collect_signals(context, action, results)
                    context.signals.update(signals)

                # Log step completion
                if self._event_logger:
                    step_event = AgentTraceEvent(
                        event_type="agent.step.complete",
                        app_id=self.config.app_id,
                        session_id=self._session_id,
                        trace_id=self._trace_id,
                        step_id=step,
                        duration_ms=int((time.time() - step_start) * 1000),
                        action_type=action.type.value if action.type else None,
                        tool_calls=(
                            [tc.name for tc in action.tool_calls]
                            if action.tool_calls
                            else None
                        ),
                    )
                    self._event_logger.emit(step_event)

                    # Render step complete
                    if self._chain_renderer:
                        self._chain_renderer.on_step_complete(step_event)

            if max_steps_exhausted and not context.is_complete:
                context.metadata["stop_reason"] = "max_steps"
                context.add_assistant_message(
                    (
                        f"Stopped after reaching the max step limit "
                        f"({self.config.max_steps}) before finishing. "
                        "Increase `max_steps` or narrow the task."
                    ),
                    stop_reason="max_steps",
                )

            # Finish chain rendering
            if self._chain_renderer:
                self._chain_renderer.finish_trace()

            # Log agent execution complete
            if self._event_logger:
                # Extract final assistant response
                assistant_response = ""
                for msg in reversed(context.messages):
                    if msg.role == MessageRole.ASSISTANT:
                        content = msg.content
                        if isinstance(content, str):
                            assistant_response = content
                        elif isinstance(content, list):
                            # Extract text from content blocks
                            text_parts = []
                            for block in content:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "text"
                                ):
                                    text_parts.append(block.get("text", ""))
                            assistant_response = " ".join(text_parts)
                        break

                complete_event = AgentTraceEvent(
                    event_type="agent.run.complete",
                    app_id=self.config.app_id,
                    session_id=self._session_id,
                    trace_id=self._trace_id,
                    payload={"assistant_response": assistant_response},
                )
                self._event_logger.emit(complete_event)

            context.metadata["trace_id"] = self._trace_id
            context.metadata["token_usage"] = dict(self._run_token_usage)
            return Response.from_context(context)
        finally:
            # Always clear trace ID when execution completes
            clear_trace_id()

    def think(self, context: Context) -> Action:
        """Decide what action to take.

        Args:
            context: Current execution context.

        Returns:
            Action to take.
        """
        think_start = time.time()

        # Get tool definitions for the current registry.
        tool_defs = self._build_tool_definitions()

        # Get messages for the provider-neutral request
        messages = context.get_messages_for_llm()

        # Log think start
        if self._event_logger:
            think_start_event = AgentTraceEvent(
                event_type="agent.think.start",
                app_id=self.config.app_id,
                session_id=self._session_id,
                trace_id=self._trace_id,
            )
            self._event_logger.emit(think_start_event)

        system_prompt = context.system_prompt

        # Estimate tokens for compaction decision
        system_prompt_tokens = self._estimate_system_prompt_tokens(system_prompt)
        tool_defs_tokens = sum(
            self._estimate_tokens_json(t.to_debug_dict()) for t in tool_defs
        )
        messages_tokens = sum(self._estimate_tokens_json(m.to_dict()) for m in messages)
        estimated_total = system_prompt_tokens + tool_defs_tokens + messages_tokens

        # Log token usage breakdown for debugging
        if self._event_logger:
            token_breakdown_event = DebugEvent(
                event_type="agent.prompt.token_breakdown",
                app_id=self.config.app_id,
                session_id=self._session_id,
                payload={
                    "trace_id": self._trace_id,
                    "system_prompt_tokens": system_prompt_tokens,
                    "tool_definitions_tokens": tool_defs_tokens,
                    "tool_count": len(tool_defs),
                    "messages_tokens": messages_tokens,
                    "message_count": len(messages),
                    "estimated_total_tokens": estimated_total,
                },
            )
            self._event_logger.emit(token_breakdown_event)

        request = LLMRequest(
            model=self.llm.model,
            system=system_prompt,
            messages=messages,
            tools=tool_defs,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            use_prompt_caching=self.config.prompt_caching_enabled,
        )
        response = self.llm.send(request)
        # Log LLM response for debugging

        if self._event_logger:
            self._event_logger.emit(
                DebugEvent(
                    event_type="agent.llm.response",
                    app_id=self.config.app_id,
                    session_id=self._session_id,
                    payload={"response": response.provider_response},
                )
            )

        # Parse response and update context
        action = self._parse_response_to_action(response, context)

        # Extract token usage for compaction tracking
        token_usage = None
        if response.usage:
            token_usage = {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            }
        if token_usage:
            input_tokens = token_usage.get("input")
            output_tokens = token_usage.get("output")
            if input_tokens is not None:
                self._run_token_usage["input"] += int(input_tokens)
            if output_tokens is not None:
                self._run_token_usage["output"] += int(output_tokens)

        # Log think completion
        if self._event_logger:
            # Prepare tool calls detail for renderer
            tool_calls_detail = None
            if action.tool_calls:
                tool_calls_detail = [
                    {"name": tc.name, "arguments": tc.arguments}
                    for tc in action.tool_calls
                ]
            assistant_text = action.metadata.get("assistant_text")
            if assistant_text:
                assistant_text = self._truncate_assistant_text(assistant_text)

            payload: Dict[str, Any] = {}
            if tool_calls_detail:
                payload["tool_calls_detail"] = tool_calls_detail
            if assistant_text:
                payload["assistant_text"] = assistant_text

            think_event = AgentTraceEvent(
                event_type="agent.think.complete",
                app_id=self.config.app_id,
                session_id=self._session_id,
                trace_id=self._trace_id,
                duration_ms=int((time.time() - think_start) * 1000),
                action_type=action.type.value if action.type else None,
                tool_calls=(
                    [tc.name for tc in action.tool_calls] if action.tool_calls else None
                ),
                token_usage=token_usage,
                payload=payload,
            )
            self._event_logger.emit(think_event)

            # Render thinking
            if self._chain_renderer:
                self._chain_renderer.on_think_complete(think_event)

        return action

    def act(self, action: Action) -> List[ToolResult]:
        """Execute an action.

        Args:
            action: Action to execute.

        Returns:
            List of tool results.
        """
        if action.type != ActionType.TOOL_CALL:
            return []

        act_start = time.time()
        results: List[ToolResult] = []
        for tool_call in action.tool_calls:
            result = self._execute_single_tool(tool_call)
            results.append(result)

        # Log act completion
        if self._event_logger:
            act_event = AgentTraceEvent(
                event_type="agent.act.complete",
                app_id=self.config.app_id,
                session_id=self._session_id,
                trace_id=self._trace_id,
                duration_ms=int((time.time() - act_start) * 1000),
                action_type=action.type.value if action.type else None,
                tool_calls=(
                    [tc.name for tc in action.tool_calls] if action.tool_calls else None
                ),
            )
            self._event_logger.emit(act_event)

            # Render action
            if self._chain_renderer:
                self._chain_renderer.on_act_complete(act_event)

        return results

    def _execute_single_tool(self, tool_call: Any) -> ToolResult:
        """Execute a single tool call with error handling.

        Args:
            tool_call: Tool call to execute.

        Returns:
            ToolResult with the execution result or error.
        """
        try:
            tool = self.tools.get(tool_call.name)
            if tool is None:
                return ToolResult(
                    tool_call_id=tool_call.id,
                    content=f"Error: Tool '{tool_call.name}' not found",
                    is_error=True,
                )

            # Log tool call with arguments before execution
            if self._event_logger:

                tool_call_event = AgentTraceEvent(
                    event_type="agent.tool.call",
                    app_id=self.config.app_id,
                    session_id=self._session_id,
                    trace_id=self._trace_id,
                    payload={
                        "tool_name": tool_call.name,
                        "tool_call_id": tool_call.id,
                        "tool_arguments": tool_call.arguments,
                    },
                )
                self._event_logger.emit(tool_call_event)

            # Execute the tool
            result = tool.execute(tool_call.arguments)

            # Log tool result
            if self._event_logger:

                result_event = AgentTraceEvent(
                    event_type="agent.tool.result",
                    app_id=self.config.app_id,
                    session_id=self._session_id,
                    trace_id=self._trace_id,
                    payload={
                        "tool_name": tool_call.name,
                        "tool_call_id": tool_call.id,
                        "is_error": result.is_error,
                        "content_length": len(result.content) if result.content else 0,
                        "content_preview": (
                            result.content[:200] if result.content else None
                        ),
                        "metadata": dict(result.metadata or {}),
                    },
                )
                self._event_logger.emit(result_event)

            return ToolResult(
                tool_call_id=tool_call.id,
                content=result.content,
                is_error=result.is_error,
                metadata=result.metadata,
            )
        except (TypeError, ValueError, KeyError, AttributeError) as e:
            # Common errors from tool execution: wrong args, missing keys, etc.
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Error executing tool: {str(e)}",
                is_error=True,
            )
        except Exception as e:
            # Catch all other exceptions to prevent agent loop from crashing
            # This is intentionally broad to ensure robustness
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Unexpected error executing tool: {str(e)}",
                is_error=True,
            )

    def observe(
        self,
        context: Context,
        action: Action,
        results: List[ToolResult],
    ) -> Context:
        """Update context with action results.

        Args:
            context: Current context.
            action: Action that was taken.
            results: Results from executing the action.

        Returns:
            Updated context.
        """
        if action.type == ActionType.TOOL_CALL and results:
            # Add tool results to context in proper format
            tool_result_blocks = [result.to_dict() for result in results]
            context.add_message(MessageRole.TOOL, tool_result_blocks)

        return context

    def _parse_response_to_action(
        self,
        response: LLMResponse,
        context: Context,
    ) -> Action:
        """Parse LLM response into an action and update context.

        Args:
            response: LLM response object.
            context: Current execution context.

        Returns:
            Action to take based on the response.
        """
        text = response.text
        tool_calls = response.tool_calls
        blocks = [block.to_dict() for block in response.content_blocks]
        stop_reason = response.stop_reason

        tool_calls, blocks = self._sanitize_tool_calls(
            tool_calls=tool_calls,
            blocks=blocks,
            stop_reason=stop_reason,
        )

        assistant_text = text.strip()
        action_metadata = {"assistant_text": assistant_text} if assistant_text else {}

        # Store the assistant's response in context
        if blocks:
            # Store as content blocks for proper format
            context.add_message(MessageRole.ASSISTANT, blocks)

        # Determine action type
        if tool_calls:
            return Action.from_tool_calls(tool_calls, metadata=action_metadata)
        else:
            # Check stop_reason to determine if we should finish
            # When Claude sends "end_turn", it means it's done and we should finish
            if stop_reason == "end_turn" or not text:
                return Action.finish()
            else:
                # For other stop reasons (like max_tokens), treat as response
                return Action.from_response(text, metadata=action_metadata)

    def _truncate_assistant_text(self, text: str, max_len: int = 240) -> str:
        """Limit assistant text previews for renderer payloads."""
        cleaned = " ".join(text.split())
        if len(cleaned) <= max_len:
            return cleaned
        return f"{cleaned[: max_len - 3]}..."

    def _sanitize_tool_calls(
        self,
        tool_calls: List[ToolCall],
        blocks: List[Dict[str, Any]],
        stop_reason: Optional[str],
    ) -> tuple[List[ToolCall], List[Dict[str, Any]]]:
        """Filter or drop tool calls that are invalid or unsafe to execute."""
        if stop_reason == "max_tokens":
            filtered_blocks = [
                block for block in blocks if block.get("type") != "tool_call"
            ]
            return [], filtered_blocks

        if not tool_calls:
            return tool_calls, blocks

        valid_calls: List[ToolCall] = []
        invalid_ids: set[str] = set()
        for tool_call in tool_calls:
            if not self._is_tool_call_valid(tool_call):
                invalid_ids.add(tool_call.id)
                continue
            valid_calls.append(tool_call)

        if not invalid_ids:
            return valid_calls, blocks

        filtered_blocks = [
            block
            for block in blocks
            if not (block.get("type") == "tool_call" and block.get("id") in invalid_ids)
        ]
        return valid_calls, filtered_blocks

    def _is_tool_call_valid(self, tool_call: ToolCall) -> bool:
        """Check if a tool call satisfies required arguments."""
        tool = self.tools.get(tool_call.name)
        if tool is None:
            return True

        required = tool.parameters.get("required", [])
        if not required:
            return True

        args = tool_call.arguments or {}
        for field in required:
            if field not in args:
                return False
            value = args.get(field)
            if value is None:
                return False
            if isinstance(value, str) and not value.strip():
                return False
        return True

    def _collect_signals(
        self,
        context: Context,
        action: Action,
        results: List[ToolResult],
    ) -> Dict[str, Any]:
        """Collect signals from the current step.

        Args:
            context: Current context.
            action: Action that was taken.
            results: Results from executing the action.

        Returns:
            Dictionary of collected signals.
        """
        if not self._signal_collector:
            return {}

        event = {
            "context": context,
            "action": action,
            "results": results,
        }

        return self._signal_collector.collect(event)

    def _build_tool_definitions(self) -> List[LLMToolDefinition]:
        """Build tool definitions from the current tool registry.

        Returns:
            List of tool definitions in provider-neutral format.
        """
        raw_tool_defs = self.tools.to_llm_format()
        tool_defs = [
            LLMToolDefinition(
                name=str(tool_def.get("name", "")),
                description=str(tool_def.get("description", "")),
                parameters_json_schema=tool_def.get("input_schema", {}),
                metadata={
                    key: value
                    for key, value in tool_def.items()
                    if key not in {"name", "description", "input_schema"}
                },
            )
            for tool_def in raw_tool_defs
        ]

        # Log per-tool token usage for debugging
        if self._event_logger:
            tool_sizes = []
            for tool_def in tool_defs:
                tool_name = tool_def.name or "unknown"
                tool_tokens = self._estimate_tokens_json(tool_def.to_debug_dict())

                tool_sizes.append({"name": tool_name, "tokens": tool_tokens})

            # Sort by token count and log top tools
            tool_sizes.sort(key=lambda x: x["tokens"], reverse=True)
            total_tool_tokens = sum(t["tokens"] for t in tool_sizes)

            self._event_logger.emit(
                DebugEvent(
                    event_type="agent.tools.token_breakdown",
                    app_id=self.config.app_id,
                    session_id=self._session_id,
                    payload={
                        "trace_id": self._trace_id,
                        "tool_count": len(tool_defs),
                        "total_tool_tokens": total_tool_tokens,
                        "avg_tokens_per_tool": (
                            total_tool_tokens // len(tool_defs) if tool_defs else 0
                        ),
                        "top_10_largest_tools": tool_sizes[:10],
                        "all_tool_sizes": tool_sizes,
                    },
                )
            )

        return tool_defs

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count for text.

        Uses improved approximation: 1 token ~= 3.5 characters with 1.05x calibration.
        This reduces estimation error from ~11% to ~2% based on empirical analysis.

        Note: This is still an approximation. Exact counts require tiktoken or
        Anthropic's tokenizer.

        Args:
            text: Text to estimate tokens for.

        Returns:
            Estimated token count.
        """
        if not text:
            return 0

        # Improved base ratio: 3.5 chars/token instead of 4
        base_estimate = int(len(text) / 3.5)

        # Apply 1.05x calibration factor to account for JSON overhead and
        # BPE tokenization variance (based on trace f75814d0 analysis)
        return int(base_estimate * 1.05)

    def _estimate_tokens_json(self, obj: Any) -> int:
        """Estimate tokens for JSON object.

        Args:
            obj: Object to estimate tokens for.

        Returns:
            Estimated token count.
        """

        try:
            return self._estimate_tokens(json.dumps(obj))
        except (TypeError, ValueError):
            return 0

    def _estimate_system_prompt_tokens(self, system_prompt: SystemPrompt) -> int:
        """Estimate token count for system prompt."""
        if isinstance(system_prompt, str):
            return self._estimate_tokens(system_prompt)
        return self._estimate_tokens_json(system_prompt)
