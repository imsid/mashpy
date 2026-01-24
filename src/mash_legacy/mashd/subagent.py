"""Sub-agent runtime and coordinator for delegated tasks."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any, Dict, List, Optional

from ..logging import AgentTraceEvent, EventLogger
from ..memory import Memory
from .bash_session import (
    BASH_DEFAULT_TIMEOUT_SECONDS,
    BASH_TOOL_NAME,
    BashSession,
    validate_bash_command,
)
from .llm_provider import AnthropicProvider, LLMProvider
from .models import Action, AgentConfig, Context, Decision, SubAgentResult, SubAgentTask
from .telemetry import TokenUsage
from .tools import ToolRegistry, ToolResult, ToolSpec, format_tool_payload


class SubAgentRuntime:
    """Slim runtime that executes phase 2 and phase 3 for sub-agents."""

    def __init__(
        self,
        config: AgentConfig,
        tool_registry: ToolRegistry,
        memory: Memory,
        event_logger: EventLogger,
        telemetry: Optional[Any] = None,
    ) -> None:
        self._config = config
        self._tool_registry = tool_registry
        self._memory = memory
        self._event_logger = event_logger
        self._telemetry = telemetry
        self.use_bash_tool = bool(self._config.use_bash_tool)
        self.bash_working_dir = config.bash_working_dir
        self._llm: LLMProvider = AnthropicProvider(
            api_key=self._config.anthropic_api_key,
            event_logger=event_logger,
            app_id=self._config.app_id,
        )
        self._bash_session: Optional[BashSession] = None

    def execute(self, task: SubAgentTask) -> SubAgentResult:
        context = task.context
        if not context.tools:
            return SubAgentResult(
                task_id=task.task_id,
                success=False,
                output="",
                tool_calls=[],
                tool_results=[],
                tokens_used={},
                error="no_tools",
            )
        max_steps = task.max_steps or self._config.max_steps
        max_tokens = task.max_tokens or self._config.max_tokens
        usage_total = TokenUsage()
        tool_calls: List[Any] = []
        tool_results: List[Dict[str, Any]] = []
        self._update_bash_working_dir(context)
        try:
            for step_index in range(max_steps):
                step_id = step_index
                step_count = step_index + 1
                context.metadata["step_id"] = step_id
                context.metadata["step_count"] = step_count
                action = self.take_action(context, max_tokens=max_tokens)
                tool_calls.extend(action.tool_calls)
                tool_results.extend(action.tool_results)
                usage = _usage_from_dict(action.tokens_used)
                usage_total.add(usage)
                decision = self.verify_work(action, step_count, max_steps)
                if not decision.should_continue:
                    output = action.assistant_text
                    success = decision.reason == "complete"
                    error = None
                    if decision.reason == "max_steps":
                        error = "max_steps"
                    return SubAgentResult(
                        task_id=task.task_id,
                        success=success,
                        output=output,
                        tool_calls=tool_calls,
                        tool_results=tool_results,
                        tokens_used=usage_total.to_dict(),
                        error=error,
                    )
                context = self.update_context(context, action)
            return SubAgentResult(
                task_id=task.task_id,
                success=False,
                output="Reached max steps without completing.",
                tool_calls=tool_calls,
                tool_results=tool_results,
                tokens_used=usage_total.to_dict(),
                error="max_steps",
            )
        finally:
            if self._bash_session is not None:
                self._bash_session.shutdown()

    def take_action(self, context: Context, *, max_tokens: int) -> Action:
        """Phase 2: Call the LLM, parse the response, and execute tools."""

        trace_id = context.metadata.get("trace_id")
        label = context.metadata.get("agent_label") or "SubAgent"
        step_id = int(context.metadata.get("step_id", 0))
        ctx = context.metadata.get("cli_context")
        response = self._llm.create_message(
            session_id=context.session_id,
            model=self._config.model,
            system=context.system_prompt,
            messages=context.messages,
            tools=context.tools,
            max_tokens=max_tokens,
            betas=_betas_from_metadata(context.metadata),
        )
        assistant_text, tool_calls, assistant_blocks = self._llm.parse_response(
            response
        )
        usage = self._llm.extract_usage(response)
        self._emit(
            "subagent.step",
            context.session_id,
            trace_id,
            step_id,
            {"tool_calls": [call.name for call in tool_calls]},
        )
        tool_results: List[Dict[str, Any]] = []
        allowed_tool_names = _allowed_tool_names(context.tools)
        for call in tool_calls:
            self._render_step_status(
                ctx,
                step_id,
                call.name,
                call.arguments,
                label=str(label),
            )
            self._emit(
                "subagent.tool.call",
                context.session_id,
                trace_id,
                step_id,
                {"tool": call.name, "arguments": call.arguments},
            )
            if call.name not in allowed_tool_names:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.tool_id,
                        "content": "Tool not allowed for sub-agent.",
                        "is_error": True,
                    }
                )
                continue
            if call.name == BASH_TOOL_NAME:
                payload = self._handle_bash_tool(call.arguments, call.tool_id)
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

    def verify_work(self, action: Action, step: int, max_steps: int) -> Decision:
        """Phase 3: Decide whether to continue or finish."""

        if action.is_complete:
            return Decision(
                should_continue=False,
                reason="complete",
                final_reply=None,
            )
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
        working_dir = self.bash_working_dir
        if self._bash_session is None:
            self._bash_session = BashSession(working_dir)
            return self._bash_session
        if self._bash_session.working_dir != working_dir:
            self._bash_session.restart(working_dir)
        return self._bash_session

    def _restart_bash_session(self) -> None:
        working_dir = self.bash_working_dir
        if self._bash_session is None:
            self._bash_session = BashSession(working_dir)
            return
        self._bash_session.restart(working_dir)

    def _update_bash_working_dir(self, context: Context) -> None:
        metadata_dir = context.metadata.get("bash_working_dir")
        if isinstance(metadata_dir, str) and metadata_dir:
            self.bash_working_dir = metadata_dir

    def _render_step_status(
        self,
        ctx: Optional[Any],
        step_id: int,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        label: str,
    ) -> None:
        if ctx is None:
            return
        args_text = _format_tool_args(arguments)
        ctx.renderer.info(
            f"[{label}] Step {step_id}: calling {tool_name} args={args_text}"
        )

    def _emit(
        self,
        event_type: str,
        session_id: str,
        trace_id: Optional[str],
        step_id: int,
        payload: Dict[str, Any],
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
        )
        self._event_logger.emit(event)


class SubAgentCoordinator:
    """Coordinates concurrent sub-agent execution."""

    def __init__(
        self,
        config: AgentConfig,
        tool_registry: ToolRegistry,
        memory: Memory,
        event_logger: EventLogger,
        *,
        max_workers: Optional[int] = None,
    ) -> None:
        self._config = config
        self._tool_registry = tool_registry
        self._memory = memory
        self._event_logger = event_logger
        self._max_workers = max_workers or 5

    def set_tool_registry(self, tool_registry: ToolRegistry) -> None:
        self._tool_registry = tool_registry

    def execute_parallel(self, tasks: List[SubAgentTask]) -> List[SubAgentResult]:
        if not tasks:
            return []
        worker_count = min(self._max_workers, len(tasks))
        results_by_id: Dict[str, SubAgentResult] = {}
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(self._run_task, task): task.task_id for task in tasks
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:
                    task_id = futures[future]
                    results_by_id[task_id] = SubAgentResult(
                        task_id=task_id,
                        success=False,
                        output="",
                        tool_calls=[],
                        tool_results=[],
                        tokens_used={},
                        error=str(exc),
                    )
                else:
                    results_by_id[result.task_id] = result
        return [
            results_by_id[task.task_id]
            for task in tasks
            if task.task_id in results_by_id
        ]

    def _run_task(self, task: SubAgentTask) -> SubAgentResult:
        sub_config = replace(self._config, subagents_enabled=False)
        runtime = SubAgentRuntime(
            sub_config,
            self._tool_registry,
            self._memory,
            self._event_logger,
        )
        return runtime.execute(task)


def _allowed_tool_names(tools: List[Dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if isinstance(name, str) and name:
            if name == "delegate_to_sub_agents":
                continue
            names.add(name)
    return names


def _format_tool_args(args: Dict[str, Any], *, max_chars: int = 200) -> str:
    if not args:
        return "{}"
    payload = format_tool_payload(args)
    compact = " ".join(payload.split())
    if len(compact) > max_chars:
        return f"{compact[:max_chars].rstrip()}..."
    return compact


def _invoke_tool(
    tool: ToolSpec,
    tool_name: str,
    args: Dict[str, Any],
    ctx: Optional[Any],
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


def _usage_from_dict(tokens_used: Dict[str, int]) -> TokenUsage:
    if not tokens_used:
        return TokenUsage()
    return TokenUsage(
        input_tokens=int(tokens_used.get("input_tokens", 0)),
        output_tokens=int(tokens_used.get("output_tokens", 0)),
    )


def _betas_from_metadata(metadata: Dict[str, Any]) -> Optional[List[str]]:
    raw = metadata.get("tool_betas")
    if not isinstance(raw, list):
        return None
    betas = [str(item) for item in raw if item]
    return betas or None
