"""Agent runtime for orchestrating tool-driven workflows."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from ..context import CLIContext
from ..logging import AgentTraceEvent, EventLogger
from ..memory import Memory
from .bash_session import (
    BASH_DEFAULT_TIMEOUT_SECONDS,
    BASH_TOOL_NAME,
    BASH_TOOL_TYPE,
    BashSession,
    validate_bash_command,
)
from .llm_provider import AnthropicProvider, LLMProvider
from .models import (
    Action,
    AgentConfig,
    AgentReply,
    AgentStep,
    Context,
    Decision,
    SubAgentTask,
)
from .runtime_tools import MemoryTool
from .subagent import SubAgentCoordinator
from .telemetry import TelemetryCollector, TokenUsage
from .tools import ToolRegistry, ToolResult, ToolSpec, format_tool_payload

_TOOL_SEARCH_TOOL_TYPE = "tool_search_tool_bm25_20251119"
_TOOL_SEARCH_TOOL_NAME = "tool_search_tool_bm25"
_TOOL_SEARCH_BETAS = ("advanced-tool-use-2025-11-20",)
_SUBAGENT_TOOL_NAME = "delegate_to_sub_agents"


class AgentRuntime:
    """Agent runtime that performs tool-driven orchestration."""

    def __init__(
        self,
        session_id: str,
        config: AgentConfig,
        tool_registry: ToolRegistry,
        memory: Memory,
        event_logger: EventLogger,
        telemetry: TelemetryCollector,
    ) -> None:
        self._session_id = session_id
        self._config = config
        self._tool_registry = tool_registry
        self._memory = memory
        self._telemetry = telemetry
        self._event_logger = event_logger
        self.use_bash_tool = bool(config.use_bash_tool)
        self.bash_working_dir = config.bash_working_dir
        self._runtime_tools = MemoryTool(memory, self._config.app_id)
        self._llm: LLMProvider = AnthropicProvider(
            api_key=self._config.anthropic_api_key,
            event_logger=event_logger,
            app_id=self._config.app_id,
        )
        self._system_prompt = self._build_system_prompt()
        self._tool_defs: List[Dict[str, Any]] = []
        self._tool_betas: List[str] = []
        self._bash_session: Optional[BashSession] = None
        self._subagent_coordinator: Optional[SubAgentCoordinator] = None
        if self._config.subagents_enabled:
            self._subagent_coordinator = SubAgentCoordinator(
                self._config,
                self._tool_registry,
                self._memory,
                self._event_logger,
            )

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def telemetry(self) -> TelemetryCollector:
        return self._telemetry

    def set_tool_registry(self, tool_registry: ToolRegistry) -> None:
        """Replace the tool registry used for MCP/tool invocations."""

        self._tool_registry = tool_registry
        if self._subagent_coordinator is not None:
            self._subagent_coordinator.set_tool_registry(tool_registry)

    def start_session(self) -> str:
        session_id = self._session_id
        self.refresh_prompt()
        self.refresh_tools(session_id)
        return f"{session_id}-{int(time.time() * 1000)}"

    def handle_message(
        self,
        session_id: str,
        agent_trace_id: str,
        text: str,
        *,
        ctx: Optional[CLIContext] = None,
    ) -> AgentReply:
        trace_id = agent_trace_id
        steps: List[AgentStep] = []
        usage_total = TokenUsage()
        self._emit(
            "agent.start",
            session_id,
            trace_id,
            0,
            {"text": text, "trace_id": trace_id},
        )
        context = self.gather_context(session_id, text, trace_id, ctx)
        max_steps = max(1, self._config.max_steps)
        step_count = 0
        for step_index in range(max_steps):
            step_id = step_index
            step_count = step_index + 1
            context.metadata["step_id"] = step_id
            context.metadata["step_count"] = step_count
            action = self.take_action(context)
            usage = _usage_from_dict(action.tokens_used)
            usage_total.add(usage)
            steps.append(
                AgentStep(step_id=step_id, tool_calls=action.tool_calls, usage=usage)
            )
            decision = self.verify_work(action, step_count)
            if not decision.should_continue:
                if decision.final_reply is not None:
                    return decision.final_reply
                if decision.reason == "complete":
                    self._emit(
                        "agent.finish",
                        session_id,
                        trace_id,
                        step_id,
                        {
                            "assistant_text": action.assistant_text,
                            "usage": usage_total.to_dict(),
                        },
                    )
                    return AgentReply(
                        text=action.assistant_text,
                        steps=steps,
                        usage=usage_total,
                        trace_id=trace_id,
                    )
                return self.handle_max_steps_exceeded(
                    context,
                    steps,
                    usage_total,
                )
            context = self.update_context(context, action)

        return self.handle_max_steps_exceeded(context, steps, usage_total)

    def gather_context(
        self,
        session_id: str,
        text: str,
        trace_id: str,
        ctx: Optional[CLIContext],
    ) -> Context:
        """Phase 1: Gather all context needed for the next action."""

        history = self._memory.get_conversation(
            self._config.app_id,
            session_id,
            limit=self._config.max_history_messages,
        )
        messages = _build_messages(history)
        if text:
            last = messages[-1] if messages else None
            if (
                last is None
                or last.get("role") != "user"
                or last.get("content") != text
            ):
                messages.append({"role": "user", "content": text})
        self.refresh_prompt()
        if not self._tool_defs:
            self.refresh_tools(session_id)
        metadata = {"trace_id": trace_id, "cli_context": ctx}
        return Context(
            session_id=session_id,
            messages=messages,
            tools=list(self._tool_defs),
            system_prompt=self._system_prompt,
            metadata=metadata,
        )

    def take_action(self, context: Context) -> Action:
        """Phase 2: Call the LLM, parse the response, and execute tools."""

        trace_id = context.metadata.get("trace_id")
        step_id = int(context.metadata.get("step_id", 0))
        ctx = context.metadata.get("cli_context")
        start = time.time()
        response = self._llm.create_message(
            session_id=context.session_id,
            model=self._config.model,
            system=context.system_prompt,
            messages=context.messages,
            tools=context.tools,
            max_tokens=self._config.max_tokens,
            betas=self._tool_betas,
        )
        latency_ms = int((time.time() - start) * 1000)
        assistant_text, tool_calls, assistant_blocks = self._llm.parse_response(
            response
        )
        usage = self._llm.extract_usage(response)
        self._telemetry.record_request(context.session_id, usage)
        self._emit(
            "agent.step",
            context.session_id,
            trace_id,
            step_id,
            {
                "tool_calls": [call.name for call in tool_calls],
                "latency_ms": latency_ms,
                "assistant_text": assistant_text,
            },
            duration_ms=latency_ms,
        )
        tool_results: List[Dict[str, Any]] = []
        for call in tool_calls:
            self._render_step_status(ctx, step_id, call.name, call.arguments)
            self._emit(
                "tool.call",
                context.session_id,
                trace_id,
                step_id,
                {"tool": call.name, "arguments": call.arguments},
            )
            if call.name == _SUBAGENT_TOOL_NAME:
                payload = self._handle_delegate_tool(
                    context, call.arguments, call.tool_id
                )
                self._emit(
                    "tool.result",
                    context.session_id,
                    trace_id,
                    step_id,
                    {"tool": call.name, "is_error": payload.get("is_error", False)},
                )
                tool_results.append(payload)
                continue
            if call.name == BASH_TOOL_NAME:
                payload = self._handle_bash_tool(call.arguments, call.tool_id)
                self._emit(
                    "tool.result",
                    context.session_id,
                    trace_id,
                    step_id,
                    {"tool": call.name, "is_error": payload.get("is_error", False)},
                )
                tool_results.append(payload)
                continue

            tool = self._tool_registry.get(call.name)
            if tool is None:
                result = ToolResult(
                    name=call.name,
                    content="Unknown tool.",
                    is_error=True,
                )
            else:
                result = _invoke_tool(tool, call.name, call.arguments, ctx)
            self._emit(
                "tool.result",
                context.session_id,
                trace_id,
                step_id,
                {"tool": call.name, "is_error": result.is_error},
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.tool_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
            )
        return Action(
            assistant_text=assistant_text,
            tool_calls=tool_calls,
            tool_results=tool_results,
            tokens_used=usage.to_dict(),
            is_complete=not tool_calls,
            assistant_blocks=assistant_blocks,
        )

    def verify_work(self, action: Action, step: int) -> Decision:
        """Phase 3: Decide whether to continue or finish."""

        if action.is_complete:
            return Decision(
                should_continue=False,
                reason="complete",
                final_reply=None,
            )
        max_steps = max(1, self._config.max_steps)
        if step >= max_steps:
            return Decision(
                should_continue=False,
                reason="max_steps",
                final_reply=None,
            )
        has_error = any(result.get("is_error") for result in action.tool_results)
        if has_error:
            return Decision(
                should_continue=True,
                reason="tool_error",
                final_reply=None,
            )
        return Decision(
            should_continue=True,
            reason="tools_executed",
            final_reply=None,
        )

    def update_context(self, context: Context, action: Action) -> Context:
        """Append assistant/tool results to the message history."""

        context.messages.append(
            {"role": "assistant", "content": action.assistant_blocks}
        )
        if action.tool_results:
            context.messages.append({"role": "user", "content": action.tool_results})
        return context

    def handle_max_steps_exceeded(
        self,
        context: Context,
        steps: List[AgentStep],
        usage_total: TokenUsage,
    ) -> AgentReply:
        """Fallback when the agent exceeds the configured max steps."""

        step_id = int(context.metadata.get("step_count", 0))
        trace_id = context.metadata.get("trace_id") or ""
        fallback_text = "Reached max steps without completing."
        self._emit(
            "agent.finish",
            context.session_id,
            trace_id,
            step_id,
            {"trace_id": trace_id, "usage": usage_total.to_dict()},
        )
        return AgentReply(
            text=fallback_text,
            steps=steps,
            usage=usage_total,
            trace_id=str(trace_id),
        )

    def _build_system_prompt(self) -> str:
        tool_search_name = (
            _TOOL_SEARCH_TOOL_NAME if self._config.tool_search_enabled else ""
        )
        parts: List[str] = [
            _default_system_prompt(
                tool_search_name=tool_search_name,
            )
        ]
        if self._config.system_prompt:
            parts.append(self._config.system_prompt.strip())
        return "\n\n".join(part for part in parts if part)

    def refresh_prompt(self) -> None:
        """Rebuild the system prompt from the latest config."""

        self._system_prompt = self._build_system_prompt()

    def refresh_tools(self, session_id: str) -> None:
        """Rebuild tool definitions and beta flags for the current session."""

        for tool in self._runtime_tools.build_tools(session_id):
            self._tool_registry.register(tool)
        tool_defs = self._tool_registry.to_anthropic_tools(
            enable_search=self._config.tool_search_enabled
        )
        if self._config.tool_search_enabled:
            tool_defs.insert(
                0,
                {"type": _TOOL_SEARCH_TOOL_TYPE, "name": _TOOL_SEARCH_TOOL_NAME},
            )
        if self.use_bash_tool:
            tool_defs.append({"type": BASH_TOOL_TYPE, "name": BASH_TOOL_NAME})
        else:
            if self._bash_session is not None:
                self._bash_session.shutdown()
                self._bash_session = None
        if self._config.subagents_enabled:
            tool_defs.append(_build_delegate_tool_def())
        self._tool_defs = tool_defs
        self._tool_betas = (
            list(_TOOL_SEARCH_BETAS) if self._config.tool_search_enabled else []
        )

    def _handle_bash_tool(
        self, args: Dict[str, Any], tool_use_id: str
    ) -> Dict[str, Any]:
        if not isinstance(args, dict):
            args = {}
        if not self.use_bash_tool:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "Bash tool is disabled.",
                "is_error": True,
            }
        if args.get("restart"):
            self._restart_bash_session()
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "Bash session restarted",
                "is_error": False,
            }
        command = str(args.get("command") or "")
        if not command.strip():
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "bash tool requires a command.",
                "is_error": True,
            }
        ok, reason = validate_bash_command(command)
        if not ok:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": reason or "Command blocked by policy.",
                "is_error": True,
            }
        try:
            session = self._get_bash_session()
            output, exit_code = session.execute_command(
                command, timeout=BASH_DEFAULT_TIMEOUT_SECONDS
            )
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": output,
                "is_error": exit_code != 0,
            }
        except TimeoutError:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": (
                    f"Command timed out after {BASH_DEFAULT_TIMEOUT_SECONDS} seconds"
                ),
                "is_error": True,
            }
        except Exception as exc:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": f"Bash tool error: {exc}",
                "is_error": True,
            }

    def _get_bash_session(self) -> BashSession:
        working_dir = self.bash_working_dir or os.getcwd()
        if self._bash_session is None:
            self._bash_session = BashSession(working_dir)
            return self._bash_session
        if self._bash_session.working_dir != working_dir:
            self._bash_session.restart(working_dir)
        return self._bash_session

    def _restart_bash_session(self) -> None:
        working_dir = self.bash_working_dir or os.getcwd()
        if self._bash_session is None:
            self._bash_session = BashSession(working_dir)
            return
        self._bash_session.restart(working_dir)

    def _emit(
        self,
        event_type: str,
        session_id: str,
        trace_id: Optional[str],
        step_id: int,
        payload: Dict[str, Any],
        *,
        duration_ms: Optional[int] = None,
    ) -> None:
        if not self._event_logger:
            return
        event = AgentTraceEvent(
            event_type=event_type,
            app_id=self._config.app_id,
            session_id=session_id,
            trace_id=trace_id,
            step_id=step_id,
            payload=payload,
            duration_ms=duration_ms,
        )
        self._event_logger.emit(event)

    def _render_step_status(
        self,
        ctx: Optional[CLIContext],
        step_id: int,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> None:
        if ctx is None:
            return
        args_text = _format_tool_args(arguments)
        ctx.renderer.info(
            f"[Main] Step {step_id}: calling {tool_name} args={args_text}"
        )

    def _render_delegate_status(
        self,
        ctx: Optional[CLIContext],
        execution_mode: str,
        tasks: List[List[str]],
        purpose: str,
    ) -> None:
        if ctx is None:
            return
        header = (
            f"[Main] Spawning {len(tasks)} sub-agents " f"({execution_mode} execution)."
        )
        if purpose:
            header = f"{header} Purpose: {purpose}"
        ctx.renderer.info(header)
        if tasks:
            ctx.renderer.table(["Sub-agent", "Goal"], tasks)

    def _handle_delegate_tool(
        self,
        context: Context,
        args: Dict[str, Any],
        tool_use_id: str,
    ) -> Dict[str, Any]:
        if not self._config.subagents_enabled or self._subagent_coordinator is None:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "Sub-agents are disabled.",
                "is_error": True,
            }
        if not isinstance(args, dict):
            args = {}
        tasks_payload = args.get("tasks")
        if not isinstance(tasks_payload, list) or not tasks_payload:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "delegate_to_sub_agents requires a non-empty tasks list.",
                "is_error": True,
            }
        execution_mode = str(args.get("execution_mode") or "parallel")
        if execution_mode != "parallel":
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "Only parallel execution_mode is supported in v1.",
                "is_error": True,
            }
        sub_tasks: List[SubAgentTask] = []
        display_rows: List[List[str]] = []
        errors: List[str] = []
        purpose = str(
            args.get("purpose") or args.get("reasoning") or args.get("reason") or ""
        ).strip()
        for index, task_data in enumerate(tasks_payload):
            if not isinstance(task_data, dict):
                continue
            task_id = str(task_data.get("task_id") or f"task_{index + 1}")
            instruction = str(task_data.get("instruction") or "").strip()
            if not instruction:
                continue
            display_rows.append([task_id, _truncate_text(instruction, 80)])
            extra_context = task_data.get("context")
            if not isinstance(extra_context, dict):
                extra_context = {}
            bash_working_dir = task_data.get("bash_working_dir")
            if not isinstance(bash_working_dir, str):
                bash_working_dir = ""
            message_text = instruction
            if extra_context:
                message_text = (
                    f"{instruction}\n\nContext:\n{json.dumps(extra_context, indent=2)}"
                )
            messages = list(context.messages)
            messages.append({"role": "user", "content": message_text})
            tools = _build_subagent_tools(context.tools)
            if not tools:
                errors.append(f"{task_id}: no tools available for sub-agent")
                continue
            metadata = dict(context.metadata)
            metadata.update(
                {
                    "parent_session_id": context.session_id,
                    "subagent_task_id": task_id,
                    "agent_label": f"SubAgent:{task_id}",
                }
            )
            if self._tool_betas:
                metadata["tool_betas"] = list(self._tool_betas)
            if bash_working_dir:
                metadata["bash_working_dir"] = bash_working_dir
            sub_context = Context(
                session_id=f"{context.session_id}-sub-{task_id}",
                messages=messages,
                tools=tools,
                system_prompt=context.system_prompt,
                metadata=metadata,
            )
            sub_tasks.append(
                SubAgentTask(
                    task_id=task_id,
                    context=sub_context,
                    max_steps=_safe_int(task_data.get("max_steps")),
                    max_tokens=_safe_int(task_data.get("max_tokens")),
                )
            )
        if errors:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "Invalid sub-agent tasks: " + "; ".join(errors),
                "is_error": True,
            }
        if not sub_tasks:
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "No valid sub-agent tasks were provided.",
                "is_error": True,
            }
        ctx = context.metadata.get("cli_context")
        self._render_delegate_status(ctx, execution_mode, display_rows, purpose)
        results = self._subagent_coordinator.execute_parallel(sub_tasks)
        payload = [
            {
                "task_id": result.task_id,
                "success": result.success,
                "output": result.output,
                "error": result.error,
                "tokens_used": result.tokens_used,
            }
            for result in results
        ]
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": format_tool_payload({"results": payload}),
            "is_error": any(not result.success for result in results),
        }


def _invoke_tool(
    tool: ToolSpec,
    tool_name: str,
    args: Dict[str, Any],
    ctx: Optional[CLIContext],
) -> ToolResult:
    try:
        result = tool.invoke(args, ctx)
    except Exception as exc:
        return ToolResult(
            name=tool_name,
            content=f"Tool error: {exc}",
            raw=exc,
            is_error=True,
        )
    if isinstance(result, ToolResult):
        return result
    if isinstance(result, str):
        return ToolResult(name=tool_name, content=result)
    if result is None:
        return ToolResult(name=tool_name, content="ok")
    return ToolResult(
        name=tool_name,
        content=format_tool_payload(result),
        raw=result,
    )


def _format_tool_args(args: Dict[str, Any], *, max_chars: int = 200) -> str:
    if not args:
        return "{}"
    payload = format_tool_payload(args)
    compact = " ".join(payload.split())
    if len(compact) > max_chars:
        return f"{compact[:max_chars].rstrip()}..."
    return compact


def _build_messages(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    for item in history:
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"}:
            continue
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
    return messages


def _build_delegate_tool_def() -> Dict[str, Any]:
    return {
        "name": _SUBAGENT_TOOL_NAME,
        "description": (
            "Delegate work to 2-5 sub-agents that execute independently in parallel. "
            "Each sub-agent receives the same tools as the main agent and runs its own exploration loop. "
            "\n\n"
            "WHEN TO USE:\n"
            "• Task splits into independent subtasks (analyzing different files, repos, or code areas)\n"
            "• Subtasks don't depend on each other's results\n"
            "• Parallel execution would be faster than sequential\n"
            "• Processing multiple similar items (files, modules, endpoints)\n"
            "\n"
            "WHEN NOT TO USE:\n"
            "• Tasks must run sequentially (one depends on another's output)\n"
            "• Request is simple enough to handle directly in 1-2 tool calls\n"
            "• Subtasks need to coordinate or share state\n"
            "\n"
            "IMPORTANT:\n"
            "• Write SPECIFIC instructions - tell sub-agents exactly what to do and which tools to use\n"
            "• Each sub-agent can use bash, memory tools, and any MCP tools you have access to\n"
            "• Sub-agents execute independently - they cannot see each other's results\n"
            "• You will receive all results to synthesize into a final answer\n"
            "\n"
            "EXAMPLES:\n"
            "Good: 'Analyze authentication in 3 microservices' → 3 parallel sub-agents, one per service\n"
            "Good: 'Check for TODO comments across 10 modules' → Multiple sub-agents processing batches\n"
            "Bad: 'Find auth files, read them, then summarize' → Sequential steps, do it yourself\n"
            "Bad: 'Answer user's question' → Too vague, handle directly"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "purpose": {
                    "type": "string",
                    "description": (
                        "Explain WHY you're delegating and HOW it will help. "
                        "Examples: 'Analyze 3 repos in parallel for speed', "
                        "'Split into focused explorations of auth, API, and database layers'"
                    ),
                },
                "execution_mode": {
                    "type": "string",
                    "enum": ["parallel"],
                    "description": "Execution mode. Currently only 'parallel' is supported.",
                },
                "tasks": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 5,
                    "description": (
                        "List of 2-5 independent subtasks. Each task runs as a separate sub-agent. "
                        "Write clear, specific instructions that tell the sub-agent exactly what to do."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": (
                                    "Short, unique identifier for this subtask. "
                                    "Use descriptive names like 'analyze_auth_service', 'check_api_routes', 'scan_database_layer'. "
                                    "This will appear in logs and results."
                                ),
                            },
                            "instruction": {
                                "type": "string",
                                "description": (
                                    "Clear, specific instruction for this sub-agent. Be explicit about what to do. "
                                    "GOOD: 'Use bash to search for all authentication-related files in src/auth/, "
                                    "then read the main AuthHandler class and describe how login works.' "
                                    "BAD: 'Look at authentication' (too vague). "
                                    "Include specific commands or search patterns when relevant."
                                ),
                            },
                            "context": {
                                "type": "object",
                                "description": (
                                    "Optional: Additional context specific to this subtask. "
                                    "Examples: {'focus_area': 'password_handling', 'file_pattern': '*.ts'}"
                                ),
                            },
                            "max_steps": {
                                "type": "integer",
                                "default": 5,
                                "minimum": 1,
                                "maximum": 10,
                                "description": (
                                    "Maximum agent loop iterations for this sub-agent. "
                                    "Default is 5. Increase for complex tasks, decrease for simple ones."
                                ),
                            },
                            "max_tokens": {
                                "type": "integer",
                                "default": 4096,
                                "minimum": 1024,
                                "maximum": 8192,
                                "description": (
                                    "Maximum tokens for this sub-agent's responses. "
                                    "Default is 4096. Reduce for simple tasks to save costs."
                                ),
                            },
                            "bash_working_dir": {
                                "type": "string",
                                "description": (
                                    "Optional: Working directory for bash commands in this subtask. "
                                    "Useful when sub-agents need to work in different directories. "
                                    "If not specified, uses the main agent's working directory."
                                ),
                            },
                        },
                        "required": ["task_id", "instruction"],
                    },
                },
            },
            "required": ["purpose", "execution_mode", "tasks"],
        },
    }


def _build_subagent_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not tools:
        return []
    return [tool for tool in tools if tool.get("name") != _SUBAGENT_TOOL_NAME]


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars].rstrip()}..."


def _usage_from_dict(tokens_used: Dict[str, int]) -> TokenUsage:
    if not tokens_used:
        return TokenUsage()
    return TokenUsage(
        input_tokens=int(tokens_used.get("input_tokens", 0)),
        output_tokens=int(tokens_used.get("output_tokens", 0)),
    )


def _default_system_prompt(
    *,
    tool_search_name: str,
) -> str:
    tool_search_line = ""
    if tool_search_name:
        tool_search_line = (
            f"You also have {tool_search_name} for discovering tools by name "
            "or description when you are unsure what to call. "
            f"Use {tool_search_name} if you are not confident about the right "
            "tool name."
        )
    parts = [
        "You are a Mash Agent running inside a Mash CLI app.",
        "Your job is to understand the user's request, use available tools strategically, "
        "and respond clearly and efficiently.",
        # Tool usage guidance
        "Available tools are listed in the tool definitions provided with each request. "
        "Always use tools to gather information rather than making assumptions.",
        # Memory tools
        "MEMORY TOOLS:",
        "- get_full_conversation: Retrieve complete conversation history",
        "- get_preferences: Retrieve saved user preferences and context",
        "- set_preferences: Save user preferences you infer from conversation (role, coding style, etc.)",
        "- list_app_data: Review stored app-specific data and insights",
        "- set_app_data: Store summaries, findings, and insights for reuse (use stable keys like 'feature_maps', 'repo_structure')",
        # Delegation guidance
        "DELEGATION WITH SUB-AGENTS:",
        "Use delegate_to_sub_agents when:",
        "- A task can be split into 2-5 independent subtasks that don't depend on each other",
        "- Different aspects of the work require focused exploration (e.g., analyzing different files, repos, or code areas)",
        "- You need to process multiple similar items concurrently (e.g., analyzing multiple files)",
        "- The work would benefit from parallel execution for speed",
        "When delegating:",
        "1. Create clear, specific instructions for each sub-agent - be explicit about what to do",
        "2. Each sub-agent gets the SAME tools you have - don't try to restrict tools unless necessary",
        "3. Each sub-agent should have a focused, independent task (avoid dependencies between sub-agents)",
        "4. Limit to 5 sub-agents maximum for resource management",
        "5. After receiving sub-agent results, synthesize them into a coherent answer",
        "DO NOT delegate when:",
        "- The task requires sequential steps that build on each other",
        "- You need context from one step to decide the next step",
        "- The task is simple enough to complete yourself in 1-2 tool calls",
        "- Sub-tasks would need to communicate with each other",
        # Best practices
        "BEST PRACTICES:",
        "- Check get_preferences first for saved context (feature maps, user role, repo info)",
        "- Use set_preferences to save useful information for future conversations",
        "- Use set_app_data to cache findings that won't change often (repo structure, API patterns)",
        "- Be efficient: don't repeat work, reuse cached information when valid",
        "- Provide specific file paths, line numbers, and code snippets when relevant",
    ]
    if tool_search_line:
        parts.append(tool_search_line)
    parts.append("App-specific guidance follows.")
    return " ".join(parts).strip()
