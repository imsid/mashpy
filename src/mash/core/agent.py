"""Core agent execution loop."""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..logging import AgentTraceEvent, DebugEvent, clear_trace_id, set_trace_id
from .config import (
    TOOL_SEARCH_BETAS,
    TOOL_SEARCH_TOOL_NAME,
    TOOL_SEARCH_TOOL_TYPE,
    AgentConfig,
)
from .context import Action, ActionType, Context, MessageRole, Response, ToolResult
from .llm import LLMProvider

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
        self.config = config
        self._signal_collector: Optional[SignalCollector] = None
        self._event_logger: Optional[EventLogger] = None
        self._session_id: Optional[str] = None
        self._trace_id: Optional[str] = None
        self._chain_renderer: Optional[Any] = None  # ChainOfThoughtRenderer

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
        if self._event_logger:
            self._trace_id = str(uuid.uuid4())
            # Set trace ID in thread-local context for cross-component correlation
            set_trace_id(self._trace_id)

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

            for step in range(self.config.max_steps):
                step_start = time.time()

                # Think: decide what to do next
                action = self.think(context)

                # Check if we're done
                if action.type == ActionType.FINISH:
                    context.mark_complete()
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
                                if isinstance(block, dict) and block.get("type") == "text":
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

        # Log think start
        if self._event_logger:
            think_start_event = AgentTraceEvent(
                event_type="agent.think.start",
                app_id=self.config.app_id,
                session_id=self._session_id,
                trace_id=self._trace_id,
            )
            self._event_logger.emit(think_start_event)

        # Get tool definitions with tool search support
        tool_defs = self._build_tool_definitions()

        # Get messages for LLM
        messages = context.get_messages_for_llm()

        system_prompt = context.system_prompt

        # Add tool search guidance if enabled
        if self.config.tool_search_enabled:
            system_prompt = self._add_tool_search_guidance(system_prompt)

        # Get beta flags for LLM request
        betas = self._get_betas()

        # Log token usage breakdown for debugging
        if self._event_logger:
            # Estimate tokens for each component
            system_prompt_tokens = self._estimate_tokens(system_prompt)
            tool_defs_tokens = sum(self._estimate_tokens_json(t) for t in tool_defs)
            messages_tokens = sum(self._estimate_tokens_json(m) for m in messages)
            estimated_total = system_prompt_tokens + tool_defs_tokens + messages_tokens

            # Log token breakdown
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

        # Call LLM
        response = self.llm.create_message(
            model=self.config.model,
            system=system_prompt,
            messages=messages,
            tools=tool_defs,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            betas=betas,
            use_prompt_caching=self.config.prompt_caching_enabled,
        )

        # Parse response and update context
        action = self._parse_response_to_action(response, context)

        # Log think completion
        if self._event_logger:
            # Extract token usage from response if available
            token_usage = None
            if hasattr(response, "usage"):
                token_usage = {
                    "input": getattr(response.usage, "input_tokens", None),
                    "output": getattr(response.usage, "output_tokens", None),
                }

            # Prepare tool calls detail for renderer
            tool_calls_detail = None
            if action.tool_calls:
                tool_calls_detail = [
                    {"name": tc.name, "arguments": tc.arguments} for tc in action.tool_calls
                ]

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
                payload={"tool_calls_detail": tool_calls_detail} if tool_calls_detail else {},
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
                from ..logging import AgentTraceEvent

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
                from ..logging import AgentTraceEvent

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
                        "content_preview": result.content[:200] if result.content else None,
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
            context.add_message(MessageRole.USER, tool_result_blocks)

        return context

    def _parse_response_to_action(
        self,
        response: Any,
        context: Context,
    ) -> Action:
        """Parse LLM response into an action and update context.

        Args:
            response: LLM response object.
            context: Current execution context.

        Returns:
            Action to take based on the response.
        """
        # Parse response
        text, tool_calls, blocks = self.llm.parse_response(response)

        # Store the assistant's response in context
        if blocks:
            # Store as content blocks for proper format
            context.add_message(MessageRole.ASSISTANT, blocks)

        # Determine action type
        if tool_calls:
            return Action.from_tool_calls(tool_calls)
        else:
            # Check stop_reason to determine if we should finish
            # When Claude sends "end_turn", it means it's done and we should finish
            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "end_turn" or not text:
                return Action.finish()
            else:
                # For other stop reasons (like max_tokens), treat as response
                return Action.from_response(text)

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

    def _format_examples(self, examples: List[Dict[str, Any]]) -> str:
        """Format examples for inclusion in system prompt.

        Args:
            examples: List of high-quality examples.

        Returns:
            Formatted string for system prompt.
        """
        if not examples:
            return ""

        lines = ["EXAMPLES FROM HIGH-PERFORMING INTERACTIONS:"]
        for i, example in enumerate(examples, 1):
            lines.append(f"\nExample {i}:")
            lines.append(f"Query: {example.get('user_message', '')}")
            lines.append(f"Response: {example.get('agent_response', '')}")

        return "\n".join(lines)

    def _build_tool_definitions(self) -> List[Dict[str, Any]]:
        """Build tool definitions, optionally including tool search.

        Returns:
            List of tool definitions in Anthropic format.
        """
        # Get base tool definitions
        tool_defs = self.tools.to_llm_format()

        # Add tool search if enabled
        if self.config.tool_search_enabled:
            # Mark tools for deferred loading, EXCEPT critical tools
            # Critical tools (bash, runtime tools) need full definitions always available
            critical_tools = {
                "bash",  # Essential for local repo exploration
                "get_conversation",  # Runtime memory tools
                "get_preferences",
                "set_preferences",
                "get_app_data",
                "set_app_data",
                "list_app_data",
                "delete_app_data",
            }

            for tool_def in tool_defs:
                tool_name = tool_def.get("name", "")
                # Only defer non-critical tools (e.g., MCP GitHub tools)
                if tool_name not in critical_tools:
                    tool_def["defer_loading"] = True

            tool_search_def = {
                "type": TOOL_SEARCH_TOOL_TYPE,
                "name": TOOL_SEARCH_TOOL_NAME,
            }
            # Insert at beginning for priority
            # Note: tool_search_tool itself is NOT deferred - it's the discovery mechanism
            tool_defs.insert(0, tool_search_def)

        # Log per-tool token usage for debugging
        if self._event_logger:
            tool_sizes = []
            deferred_count = 0
            for tool_def in tool_defs:
                tool_name = tool_def.get("name", "unknown")
                is_deferred = tool_def.get("defer_loading", False)

                if is_deferred:
                    # Deferred tools only send minimal metadata: name + defer_loading flag
                    # Estimate: ~15-20 tokens per deferred tool (name + boolean)
                    tool_tokens = 20
                    deferred_count += 1
                else:
                    # Non-deferred tools send full definition
                    tool_tokens = self._estimate_tokens_json(tool_def)

                tool_sizes.append({
                    "name": tool_name,
                    "tokens": tool_tokens,
                    "deferred": is_deferred
                })

            # Sort by token count and log top tools
            tool_sizes.sort(key=lambda x: x["tokens"], reverse=True)
            total_tool_tokens = sum(t["tokens"] for t in tool_sizes)

            self._event_logger.emit(DebugEvent(
                event_type="agent.tools.token_breakdown",
                app_id=self.config.app_id,
                session_id=self._session_id,
                payload={
                    "trace_id": self._trace_id,
                    "tool_count": len(tool_defs),
                    "deferred_tool_count": deferred_count,
                    "non_deferred_tool_count": len(tool_defs) - deferred_count,
                    "total_tool_tokens": total_tool_tokens,
                    "avg_tokens_per_tool": total_tool_tokens // len(tool_defs) if tool_defs else 0,
                    "tool_search_enabled": self.config.tool_search_enabled,
                    "top_10_largest_tools": tool_sizes[:10],
                    "all_tool_sizes": tool_sizes,
                },
            ))

        return tool_defs

    def _get_betas(self) -> Optional[List[str]]:
        """Get beta flags for LLM request.

        Returns:
            List of beta feature flags, or None if tool search is disabled.
        """
        if self.config.tool_search_enabled:

            return list(TOOL_SEARCH_BETAS)
        return None

    def _add_tool_search_guidance(self, system_prompt: str) -> str:
        """Add tool search guidance to system prompt.

        Args:
            system_prompt: Original system prompt.

        Returns:
            System prompt with tool search guidance appended.
        """

        tool_search_guidance = f"""

TOOL DISCOVERY:
You have access to {TOOL_SEARCH_TOOL_NAME} for discovering tools by name or description when you are unsure what to call. Use {TOOL_SEARCH_TOOL_NAME} if you are not confident about the right tool name.
"""
        return f"{system_prompt}\n{tool_search_guidance}"

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
        import json
        try:
            return self._estimate_tokens(json.dumps(obj))
        except (TypeError, ValueError):
            return 0
