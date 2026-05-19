"""Core agent execution loop."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from mash.skills.registry import SkillRegistry
from mash.skills.tool import SkillTool

from ..logging import AgentTraceEvent, clear_trace_id, set_trace_id
from ..runtime.events import RuntimeEvent, RuntimeEventType
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


@dataclass
class StepPlan:
    """One planned agent step after a think phase."""

    action: Action
    duration_ms: int
    token_usage: Dict[str, int] = field(default_factory=dict)
    tool_usage: Dict[str, Dict[str, int]] = field(default_factory=dict)
    trace_id: Optional[str] = None


@dataclass
class StepCommitResult:
    """Result of committing one agent step to context."""

    context: Context
    done: bool
    signals: Dict[str, Any] = field(default_factory=dict)


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
        self._trace_tool_usage: Dict[str, Dict[str, int]] = {}

    def set_signal_collector(self, collector: SignalCollector) -> None:
        """Set the signal collector for automatic signal collection."""
        self._signal_collector = collector

    def get_signal_definitions(self) -> Dict[str, Dict[str, Any]]:
        """Return registered signal definitions keyed by signal name."""
        if self._signal_collector is None:
            return {}
        return self._signal_collector.get_signal_definitions()

    def set_event_logger(self, logger: EventLogger, session_id: str) -> None:
        """Set the event logger for automatic event logging.

        Args:
            logger: Event logger instance.
            session_id: Session ID for this run.
        """
        self._event_logger = logger
        self._session_id = session_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        """Bind a trace ID for externally managed execution flows."""
        self._trace_id = trace_id
        if hasattr(self.llm, "set_trace_id"):
            self.llm.set_trace_id(trace_id)

    def set_trace_tool_usage(self, tool_usage: Dict[str, Dict[str, int]]) -> None:
        """Replace the accumulated per-tool trace usage for external replay flows."""
        normalized: Dict[str, Dict[str, int]] = {}
        for name, entry in (tool_usage or {}).items():
            if not isinstance(entry, dict):
                continue
            normalized[str(name)] = {
                "tokens": int(entry.get("tokens", 0) or 0),
                "invocations": int(entry.get("invocations", 0) or 0),
            }
        self._trace_tool_usage = normalized

    def get_trace_tool_usage(self) -> Dict[str, Dict[str, int]]:
        """Return a copy of the accumulated per-tool trace usage."""
        return self._get_trace_tool_usage()

    def get_event_logger_session_id(self) -> Optional[str]:
        """Return the currently bound event-logger session ID."""
        return self._session_id

    def set_chain_renderer(self, renderer: Any) -> None:
        """Set the chain of thought renderer for real-time visualization.

        Args:
            renderer: Chain of thought renderer instance.
        """
        self._chain_renderer = renderer

    async def run(self, context: Context) -> Response:
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
        self._trace_tool_usage = {}

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
            await self._event_logger.emit(start_event)

        try:
            # Start chain rendering
            if self._chain_renderer:
                self._chain_renderer.start_trace(self._trace_id)

            for step in range(self.config.max_steps):
                step_start = time.time()
                plan = await self.plan_step(context)
                results = await self.act(plan.action)
                commit = self.commit_step(
                    context,
                    plan.action,
                    results,
                    step_index=step,
                )
                context = commit.context

                if plan.action.type != ActionType.FINISH:
                    await self._emit_step_complete(
                        plan.action,
                        step_index=step,
                        duration_ms=int((time.time() - step_start) * 1000),
                    )
                if commit.done:
                    break

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
                await self._event_logger.emit(complete_event)

            context.metadata["trace_id"] = self._trace_id
            context.metadata["token_usage"] = dict(self._run_token_usage)
            return Response.from_context(context)
        finally:
            # Always clear trace ID when execution completes
            clear_trace_id()

    async def plan_step(self, context: Context) -> StepPlan:
        """Execute one think phase and return a durable step plan."""
        started_at = time.time()
        action = await self.think(context)
        return StepPlan(
            action=action,
            duration_ms=int((time.time() - started_at) * 1000),
            token_usage=dict(action.metadata.get("token_usage") or {}),
            tool_usage=self.get_trace_tool_usage(),
            trace_id=action.metadata.get("trace_id") or self._trace_id,
        )

    def commit_step(
        self,
        context: Context,
        action: Action,
        results: List[ToolResult],
        *,
        step_index: int,
        tool_usage: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> StepCommitResult:
        """Commit one planned step into context using canonical loop semantics."""
        signals: Dict[str, Any] = {}
        done = False

        if action.type == ActionType.FINISH:
            signals = self.collect_signals(
                context,
                action,
                results,
                tool_usage=tool_usage,
            )
            if signals:
                context.signals.update(signals)
            context.mark_complete()
            done = True
        elif action.type == ActionType.TOOL_CALL:
            context = self.observe(context, action, results)

        if not done and step_index + 1 >= self.config.max_steps:
            self._apply_max_steps_exhausted(context)
            done = True

        return StepCommitResult(context=context, done=done, signals=signals)

    async def think(self, context: Context) -> Action:
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
            await self._event_logger.emit(think_start_event)

        system_prompt = context.system_prompt

        request = LLMRequest(
            model=self.llm.model,
            system=system_prompt,
            messages=messages,
            tools=tool_defs,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            use_prompt_caching=self.config.prompt_caching_enabled,
        )
        response = await self.llm.send(request)

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
        if token_usage:
            action.metadata["token_usage"] = dict(token_usage)
        if self._trace_id:
            action.metadata["trace_id"] = self._trace_id

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
            await self._event_logger.emit(think_event)

            # Render thinking
            if self._chain_renderer:
                self._chain_renderer.on_runtime_event(
                    RuntimeEvent(
                        app_id=self.config.app_id,
                        agent_id=self.config.app_id,
                        session_id=self._session_id,
                        trace_id=self._trace_id,
                        event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
                        payload={
                            "duration_ms": think_event.duration_ms,
                            "action_type": think_event.action_type,
                            "tool_calls": tool_calls_detail,
                            "assistant_text": assistant_text,
                            "token_usage": token_usage,
                        },
                    )
                )

        return action

    async def act(self, action: Action) -> List[ToolResult]:
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
            result = await self.execute_tool_call(tool_call)
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
            await self._event_logger.emit(act_event)

            # Render action
            if self._chain_renderer:
                self._chain_renderer.on_runtime_event(
                    RuntimeEvent(
                        app_id=self.config.app_id,
                        agent_id=self.config.app_id,
                        session_id=self._session_id,
                        trace_id=self._trace_id,
                        event_type=RuntimeEventType.TOOL_CALL_COMPLETED.value,
                        payload={
                            "duration_ms": act_event.duration_ms,
                            "action_type": act_event.action_type,
                            "tool_calls": act_event.tool_calls,
                        },
                    )
                )

        return results

    async def execute_step_tool_call(self, tool_call: ToolCall) -> ToolResult:
        """Execute one tool call as a durable sub-boundary within a step."""
        return await self.execute_tool_call(tool_call)

    async def execute_tool_call(self, tool_call: Any) -> ToolResult:
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
                await self._event_logger.emit(tool_call_event)

            self._increment_trace_tool_invocation(tool_call.name)

            # Execute the tool
            result = await tool.execute(tool_call.arguments)

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
                await self._event_logger.emit(result_event)

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
        action_metadata: Dict[str, Any] = {}
        if assistant_text:
            action_metadata["assistant_text"] = assistant_text
        if blocks:
            action_metadata["assistant_blocks"] = blocks
        if stop_reason:
            action_metadata["stop_reason"] = stop_reason

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
                return Action.finish(metadata=action_metadata)
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
        for field_name in required:
            if field_name not in args:
                return False
            value = args.get(field_name)
            if value is None:
                return False
            if isinstance(value, str) and not value.strip():
                return False
        return True

    def collect_signals(
        self,
        context: Context,
        action: Action,
        results: List[ToolResult],
        *,
        tool_usage: Optional[Dict[str, Dict[str, int]]] = None,
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
            "tool_usage": self._normalized_tool_usage(tool_usage),
        }

        return self._signal_collector.collect(event)

    async def _emit_step_complete(
        self,
        action: Action,
        *,
        step_index: int,
        duration_ms: int,
    ) -> None:
        """Emit one step-complete event using the canonical agent step contract."""
        if not self._event_logger:
            return
        step_event = AgentTraceEvent(
            event_type="agent.step.complete",
            app_id=self.config.app_id,
            session_id=self._session_id,
            trace_id=self._trace_id,
            step_id=step_index,
            duration_ms=int(duration_ms),
            action_type=action.type.value if action.type else None,
            tool_calls=[tc.name for tc in action.tool_calls] if action.tool_calls else None,
        )
        await self._event_logger.emit(step_event)
        if self._chain_renderer:
            self._chain_renderer.on_runtime_event(
                RuntimeEvent(
                    app_id=self.config.app_id,
                    agent_id=self.config.app_id,
                    session_id=self._session_id,
                    trace_id=self._trace_id,
                    event_type=RuntimeEventType.STEP_COMPLETED.value,
                    loop_index=step_index,
                    payload={
                        "duration_ms": step_event.duration_ms,
                        "action_type": step_event.action_type,
                        "tool_calls": step_event.tool_calls,
                    },
                )
            )

    def _apply_max_steps_exhausted(self, context: Context) -> None:
        """Apply the canonical max-step exhaustion behavior."""
        context.metadata["stop_reason"] = "max_steps"
        context.add_assistant_message(
            (
                f"Stopped after reaching the max step limit "
                f"({self.config.max_steps}) before finishing. "
                "Increase `max_steps` or narrow the task."
            ),
            stop_reason="max_steps",
        )

    def _normalized_tool_usage(
        self,
        tool_usage: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> Dict[str, Dict[str, int]]:
        source = self._get_trace_tool_usage() if tool_usage is None else tool_usage
        return {
            str(name): {
                "tokens": int(entry.get("tokens", 0) or 0),
                "invocations": int(entry.get("invocations", 0) or 0),
            }
            for name, entry in dict(source or {}).items()
            if isinstance(entry, dict)
        }

    def _build_tool_definitions(self) -> List[LLMToolDefinition]:
        """Build tool definitions from the current tool registry.

        Returns:
            List of tool definitions in provider-neutral format.
        """
        raw_tool_defs = self.tools.to_llm_format()
        tool_defs: List[LLMToolDefinition] = []
        for tool_def in raw_tool_defs:
            normalized = LLMToolDefinition(
                name=str(tool_def.get("name", "")),
                description=str(tool_def.get("description", "")),
                parameters_json_schema=tool_def.get("input_schema", {}),
                metadata={
                    key: value
                    for key, value in tool_def.items()
                    if key not in {"name", "description", "input_schema"}
                },
            )
            tool_defs.append(normalized)
            tool_name = normalized.name or "unknown"
            tool_tokens = self._estimate_tokens_json(normalized.to_debug_dict())
            self._remember_trace_tool(tool_name, tool_tokens)

        return tool_defs

    def _remember_trace_tool(self, tool_name: str, tool_tokens: int) -> None:
        """Track one available tool for the current trace."""
        entry = self._trace_tool_usage.get(tool_name)
        if entry is None:
            self._trace_tool_usage[tool_name] = {
                "tokens": max(0, int(tool_tokens)),
                "invocations": 0,
            }
            return
        entry["tokens"] = max(entry["tokens"], int(tool_tokens))

    def _increment_trace_tool_invocation(self, tool_name: str) -> None:
        """Increment the invocation count for one tool in the current trace."""
        cleaned_name = str(tool_name or "").strip()
        if not cleaned_name:
            return
        entry = self._trace_tool_usage.get(cleaned_name)
        if entry is None:
            self._trace_tool_usage[cleaned_name] = {"tokens": 0, "invocations": 1}
            return
        entry["invocations"] += 1

    def _get_trace_tool_usage(self) -> Dict[str, Dict[str, int]]:
        """Return a copy of the accumulated per-tool trace usage."""
        return {
            name: {
                "tokens": int(entry.get("tokens", 0)),
                "invocations": int(entry.get("invocations", 0)),
            }
            for name, entry in self._trace_tool_usage.items()
        }

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
