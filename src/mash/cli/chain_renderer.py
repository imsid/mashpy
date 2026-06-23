"""Real-time chain of thought renderer for agent execution."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from mash.runtime.events import RuntimeEvent, RuntimeEventType, RuntimeTrace
from mash.runtime.events.trace import (
    _as_int,
    _clean_text,
    _dict_value,
    _tool_call_names,
    _tool_calls_detail,
)


class ChainOfThoughtRenderer:
    """Renders agent's chain of thought in real-time."""

    def __init__(self, console: Optional[Console] = None) -> None:
        self._console = console or Console()
        self._current_trace_id: Optional[str] = None
        self._current_trace_label: Optional[str] = None
        self._current_step: int = 0
        self._steps: List[Dict[str, Any]] = []
        self._enabled = True
        self._subagent_steps: Dict[str, List[Dict[str, Any]]] = {}
        self._subagent_step_counters: Dict[str, int] = {}
        self._subagent_headers_shown: set[str] = set()
        # Live token streaming state for llm.response.delta events.
        self._response_streaming = False
        self._response_streamed = False
        self._response_buffer = ""

    def enable(self) -> None:
        """Enable rendering."""
        self._enabled = True

    def disable(self) -> None:
        """Disable rendering."""
        self._enabled = False

    def start_trace(self, trace_id: Optional[str], label: Optional[str] = None) -> None:
        """Start a new execution trace.

        Args:
            trace_id: Unique trace identifier.
            label: Optional trace label shown in the renderer.
        """
        if not self._enabled:
            return
        if not trace_id:
            return
        if self._current_trace_id and self._current_trace_id != trace_id and self._steps:
            self._render_summary()
        self._current_trace_id = trace_id
        self._current_trace_label = label
        self._current_step = 0
        self._steps = []
        self._subagent_steps = {}
        self._subagent_step_counters = {}
        self._subagent_headers_shown = set()
        self._response_streaming = False
        self._response_streamed = False
        self._response_buffer = ""
        title = "Agent Execution Started"
        if label:
            title = f"{label} Execution Started"
        self._console.print(f"\n[bold cyan]{title}[/bold cyan]")

    def on_runtime_event(
        self,
        event: RuntimeEvent,
        *,
        trace_label: str | None = None,
    ) -> None:
        """Render one canonical runtime event."""
        if not self._enabled:
            return
        if event.event_type in {
            RuntimeEventType.LLM_THINK_STARTED.value,
            RuntimeEventType.TOOL_CALL_STARTED.value,
        }:
            return
        if event.event_type == "llm.response.delta":
            self._on_runtime_response_delta(event, trace_label=trace_label)
            return
        if event.event_type == RuntimeEventType.LLM_THINK_COMPLETED.value:
            self._on_runtime_think_complete(event, trace_label=trace_label)
            return
        if event.event_type in {
            RuntimeEventType.TOOL_CALL_COMPLETED.value,
            RuntimeEventType.SUBAGENT_CALL_COMPLETED.value,
        }:
            self._on_runtime_act_complete(event)
            return
        if event.event_type == RuntimeEventType.STEP_COMPLETED.value:
            self._on_runtime_step_complete(event)
            return
        if ".error" in event.event_type:
            self._on_runtime_error_event(event)

    def render_runtime_trace(
        self,
        trace: RuntimeTrace,
        *,
        trace_label: str | None = None,
    ) -> None:
        """Render a parsed runtime trace."""
        for raw_event in trace.events:
            self.on_runtime_event(
                RuntimeEvent(
                    event_id=int(raw_event.get("event_id") or 0),
                    request_id=raw_event.get("request_id"),
                    request_seq=raw_event.get("request_seq"),
                    trace_id=raw_event.get("trace_id"),
                    app_id=str(raw_event.get("app_id") or trace.target_agent_id),
                    agent_id=str(raw_event.get("agent_id") or trace.target_agent_id),
                    session_id=raw_event.get("session_id"),
                    event_type=str(raw_event.get("event_type") or ""),
                    loop_index=raw_event.get("loop_index"),
                    step_key=raw_event.get("step_key"),
                    payload=dict(raw_event.get("payload") or {}),
                    created_at=float(raw_event.get("created_at") or 0.0),
                ),
                trace_label=trace_label,
            )
        self.finish_trace()

    def _on_runtime_error_event(self, event: RuntimeEvent) -> None:
        """Render a warning line for any event whose type contains '.error'."""
        payload = event.payload or {}
        error = payload.get("error") or payload.get("message") or ""
        server = payload.get("server_name") or payload.get("tool_name") or ""
        label = f"[{server}] " if server else ""
        suffix = f": {error}" if error else ""
        self._console.print(
            f"  [bold yellow]⚠[/bold yellow]  {label}{event.event_type}{suffix}"
        )

    def _on_runtime_response_delta(
        self,
        event: RuntimeEvent,
        *,
        trace_label: str | None = None,
    ) -> None:
        """Render a coalesced llm.response.delta chunk live as it streams."""
        if event.trace_id != self._current_trace_id:
            self.start_trace(event.trace_id, label=trace_label)
        payload = event.payload or {}
        if not isinstance(payload, dict):
            return
        # LLMEvent fields are wrapped under "payload" by the logging layer;
        # tolerate either the wrapped or a flattened shape.
        text = payload.get("text")
        if text is None and isinstance(payload.get("payload"), dict):
            text = payload["payload"].get("text")
        if not text:
            return
        if not self._response_streaming:
            self._console.rule("[cyan]Assistant[/cyan]", style="cyan")
            self._response_streaming = True
            self._response_streamed = True
        self._response_buffer += text
        self._flush_response_markdown(final=False)

    @staticmethod
    def _split_complete_markdown(buffer: str) -> tuple[str, str]:
        """Split a streamed buffer into (renderable, remainder).

        Markdown needs whole blocks to format correctly (a code fence is not
        valid until it closes), so only return text up to the last blank-line
        block boundary that sits outside an open code fence. The remainder stays
        buffered until more text arrives.
        """
        lines = buffer.split("\n")
        fence_open = False
        boundary = 0
        for index, line in enumerate(lines):
            if line.lstrip().startswith("```"):
                fence_open = not fence_open
                continue
            if not fence_open and line.strip() == "":
                boundary = index + 1
        if boundary == 0:
            return "", buffer
        return "\n".join(lines[:boundary]), "\n".join(lines[boundary:])

    def _flush_response_markdown(self, *, final: bool) -> None:
        """Render completed markdown blocks from the streamed buffer."""
        if final:
            chunk = self._response_buffer
            self._response_buffer = ""
            if chunk.strip():
                self._console.print(Markdown(chunk))
            return
        complete, remainder = self._split_complete_markdown(self._response_buffer)
        if complete.strip():
            self._console.print(Markdown(complete))
            self._response_buffer = remainder

    def _close_response_stream(self) -> None:
        """Flush any buffered markdown and end the streamed response block."""
        if self._response_streaming:
            self._flush_response_markdown(final=True)
            self._response_streaming = False

    def response_streamed(self) -> bool:
        """Return whether response text streamed live (non-consuming peek)."""
        return self._response_streamed

    def take_response_streamed(self) -> bool:
        """Return whether response text was streamed live, resetting the flag.

        Lets the caller suppress a duplicate full-text render when the answer
        was already shown token-by-token.
        """
        streamed = self._response_streamed
        self._response_streamed = False
        return streamed

    def _on_runtime_think_complete(
        self,
        event: RuntimeEvent,
        *,
        trace_label: str | None = None,
    ) -> None:
        self._close_response_stream()
        if event.trace_id != self._current_trace_id:
            self.start_trace(event.trace_id, label=trace_label)

        payload = dict(event.payload or {})
        tool_calls_detail = _tool_calls_detail(payload)
        step_info = {
            "step": event.loop_index,
            "action_type": _clean_text(payload.get("action_type")),
            "tool_calls": _tool_call_names(tool_calls_detail),
            "tool_calls_detail": tool_calls_detail,
            "assistant_text": _clean_text(payload.get("assistant_text")),
            "token_usage": _dict_value(payload.get("token_usage")),
            "think_duration": _as_int(payload.get("duration_ms")) or 0,
        }
        if isinstance(event.loop_index, int) and event.loop_index >= 0:
            step_info["display_step"] = event.loop_index + 1
        self._steps.append(step_info)
        self._render_think(step_info)

    def _on_runtime_act_complete(self, event: RuntimeEvent) -> None:
        if not self._steps:
            return
        payload = dict(event.payload or {})
        tool_calls = _tool_call_names(payload.get("tool_calls"))
        tool_name = _clean_text(payload.get("tool_name"))
        if not tool_calls and tool_name:
            tool_calls = [tool_name]
        if tool_calls:
            self._steps[-1]["tool_calls"] = tool_calls
        self._steps[-1]["act_duration"] = _as_int(payload.get("duration_ms")) or 0
        self._render_act(self._steps[-1])

    def _on_runtime_step_complete(self, event: RuntimeEvent) -> None:
        if not self._steps:
            return
        payload = dict(event.payload or {})
        self._steps[-1]["total_duration"] = _as_int(payload.get("duration_ms")) or 0
        self._render_step_complete(self._steps[-1])
        if isinstance(event.loop_index, int) and event.loop_index >= 0:
            self._current_step = max(self._current_step, event.loop_index + 1)
        else:
            self._current_step += 1

    def on_llm_request_start(self) -> None:
        """Handle LLM request start."""

    def on_llm_request_complete(self, event: Any) -> None:
        """Handle LLM request complete."""

    def finish_trace(self) -> None:
        """Finish the current trace."""
        if not self._enabled:
            return
        self._close_response_stream()
        if self._steps:
            self._render_summary()
        self._current_trace_id = None
        self._current_trace_label = None
        self._steps = []

    def _render_think(self, step: Dict[str, Any]) -> None:
        """Render thinking phase.

        Args:
            step: Step information.
        """
        action_type = step.get("action_type", "unknown")
        tool_calls = step.get("tool_calls") or []
        tool_calls_detail = step.get("tool_calls_detail") or []
        assistant_text = step.get("assistant_text")
        token_usage = step.get("token_usage") or {}
        think_duration = step.get("think_duration", 0)

        # Build step description
        if action_type == "tool_call" and tool_calls:
            tools_str = ", ".join(f"[yellow]{t}[/yellow]" for t in tool_calls)
            desc = f"Calling tools: {tools_str}"
        elif action_type == "response":
            desc = "[green]Generating response[/green]"
        elif action_type == "finish":
            desc = "[blue]Finishing execution[/blue]"
        else:
            desc = f"Action: {action_type}"

        # Show tokens if available
        token_str = ""
        if token_usage:
            input_tok = token_usage.get("input", 0)
            output_tok = token_usage.get("output", 0)
            token_str = f" [dim]({input_tok}+{output_tok} tokens)[/dim]"

        display_step = step.get("display_step")
        if not isinstance(display_step, int) or display_step <= 0:
            display_step = self._current_step + 1

        self._console.print(
            f"  [cyan]→[/cyan] Step {display_step}: {desc}{token_str} "
            f"[dim]{think_duration}ms[/dim]"
        )

        if assistant_text and action_type not in ("response", "finish"):
            self._console.print(f"    [dim]assistant: {assistant_text}[/dim]")

        # Show tool commands/arguments if available
        if tool_calls_detail:
            for tool_call in tool_calls_detail:
                tool_name = tool_call.get("name", "unknown")
                tool_args = tool_call.get("arguments", {})

                # Special handling for bash tool to show the command
                if tool_name == "bash" and "command" in tool_args:
                    command = tool_args["command"]
                    # Truncate long commands
                    if len(command) > 80:
                        command = command[:77] + "..."
                    self._console.print(f"    [dim]$ {command}[/dim]")
                # For other tools, show arguments more generically
                elif tool_args:
                    # Show first few keys/values
                    args_preview = []
                    for key, value in list(tool_args.items())[:2]:
                        if isinstance(value, str) and len(value) > 40:
                            value = value[:37] + "..."
                        args_preview.append(f"{key}={value}")
                    if len(tool_args) > 2:
                        args_preview.append(f"+{len(tool_args) - 2} more")
                    args_str = ", ".join(args_preview)
                    self._console.print(f"    [dim]{tool_name}({args_str})[/dim]")

    def _render_act(self, step: Dict[str, Any]) -> None:
        """Render action phase.

        Args:
            step: Step information.
        """
        act_duration = step.get("act_duration", 0)
        tool_calls = step.get("tool_calls") or []

        if tool_calls:
            self._console.print(
                f"    [dim]✓ Executed {len(tool_calls)} tool(s) in {act_duration}ms[/dim]"
            )

    def _render_step_complete(self, step: Dict[str, Any]) -> None:
        """Render step completion."""

    def render_subagent_event(
        self,
        event: RuntimeEvent,
        *,
        agent_id: str,
    ) -> None:
        """Render a subagent trace event inline under the primary trace."""
        if not self._enabled:
            return
        if event.event_type in {
            RuntimeEventType.LLM_THINK_STARTED.value,
            RuntimeEventType.TOOL_CALL_STARTED.value,
        }:
            return
        if event.event_type == RuntimeEventType.LLM_THINK_COMPLETED.value:
            self._render_subagent_think(event, agent_id)
            return
        if event.event_type in {
            RuntimeEventType.TOOL_CALL_COMPLETED.value,
            RuntimeEventType.SUBAGENT_CALL_COMPLETED.value,
        }:
            self._render_subagent_act(event, agent_id)
            return

    def _render_subagent_think(self, event: RuntimeEvent, agent_id: str) -> None:
        if agent_id not in self._subagent_headers_shown:
            self._subagent_headers_shown.add(agent_id)
            self._console.print(f"    [bold magenta]┌ {agent_id}[/bold magenta]")

        payload = dict(event.payload or {})
        tool_calls_detail = _tool_calls_detail(payload)
        step_info = {
            "action_type": _clean_text(payload.get("action_type")),
            "tool_calls": _tool_call_names(tool_calls_detail),
            "tool_calls_detail": tool_calls_detail,
            "token_usage": _dict_value(payload.get("token_usage")),
            "think_duration": _as_int(payload.get("duration_ms")) or 0,
        }
        self._subagent_steps.setdefault(agent_id, []).append(step_info)
        counter = self._subagent_step_counters.get(agent_id, 0) + 1
        self._subagent_step_counters[agent_id] = counter

        action_type = step_info["action_type"] or "unknown"
        tool_calls = step_info["tool_calls"] or []
        token_usage = step_info["token_usage"] or {}
        think_duration = step_info["think_duration"]

        if action_type == "tool_call" and tool_calls:
            tools_str = ", ".join(f"[yellow]{t}[/yellow]" for t in tool_calls)
            desc = f"Calling tools: {tools_str}"
        elif action_type == "response":
            desc = "[green]Generating response[/green]"
        elif action_type == "finish":
            desc = "[blue]Finishing execution[/blue]"
        else:
            desc = f"Action: {action_type}"

        token_str = ""
        if token_usage:
            input_tok = token_usage.get("input", 0)
            output_tok = token_usage.get("output", 0)
            token_str = f" [dim]({input_tok}+{output_tok} tokens)[/dim]"

        self._console.print(
            f"    [magenta]│[/magenta] [cyan]→[/cyan] Step {counter}: {desc}{token_str} "
            f"[dim]{think_duration}ms[/dim]"
        )

        if tool_calls_detail:
            for tool_call in tool_calls_detail:
                tool_name = tool_call.get("name", "unknown")
                tool_args = tool_call.get("arguments", {})
                if tool_name == "bash" and "command" in tool_args:
                    command = tool_args["command"]
                    if len(command) > 80:
                        command = command[:77] + "..."
                    self._console.print(f"    [magenta]│[/magenta]   [dim]$ {command}[/dim]")
                elif tool_args:
                    args_preview = []
                    for key, value in list(tool_args.items())[:2]:
                        if isinstance(value, str) and len(value) > 40:
                            value = value[:37] + "..."
                        args_preview.append(f"{key}={value}")
                    if len(tool_args) > 2:
                        args_preview.append(f"+{len(tool_args) - 2} more")
                    args_str = ", ".join(args_preview)
                    self._console.print(
                        f"    [magenta]│[/magenta]   [dim]{tool_name}({args_str})[/dim]"
                    )

    def _render_subagent_act(self, event: RuntimeEvent, agent_id: str) -> None:
        payload = dict(event.payload or {})
        act_duration = _as_int(payload.get("duration_ms")) or 0
        tool_name = _clean_text(payload.get("tool_name"))
        steps = self._subagent_steps.get(agent_id)
        if steps:
            steps[-1]["act_duration"] = act_duration

        if tool_name:
            self._console.print(
                f"    [magenta]│[/magenta]   [dim]✓ {tool_name} in {act_duration}ms[/dim]"
            )
        else:
            self._console.print(
                f"    [magenta]│[/magenta]   [dim]✓ Executed in {act_duration}ms[/dim]"
            )

    def finish_subagent(self, agent_id: str, duration_ms: int) -> None:
        """Render subagent completion."""
        if not self._enabled:
            return
        if agent_id not in self._subagent_headers_shown:
            return
        self._console.print(
            f"    [bold magenta]└ {agent_id}[/bold magenta] "
            f"[dim]{duration_ms:,}ms[/dim]"
        )

    def _render_summary(self) -> None:
        """Render execution summary."""
        if not self._steps:
            return

        total_steps = len(self._steps)
        total_duration = sum(
            self._step_duration_ms(step) for step in self._steps
        )
        total_tokens = sum(
            (s.get("token_usage") or {}).get("input", 0)
            + (s.get("token_usage") or {}).get("output", 0)
            for s in self._steps
        )

        # Count tool calls
        tool_calls = []
        for step in self._steps:
            if step.get("tool_calls"):
                tool_calls.extend(step["tool_calls"])

        summary = Text()
        label = self._current_trace_label or "Agent"
        summary.append(f"\n{label} Execution Complete: ", style="bold green")
        summary.append(f"{total_steps} steps, ", style="dim")
        summary.append(f"{len(tool_calls)} tools, ", style="dim")
        summary.append(f"{total_tokens:,} tokens, ", style="dim")
        summary.append(f"{total_duration:,}ms", style="dim")

        self._console.print(summary)

    @staticmethod
    def _step_duration_ms(step: Dict[str, Any]) -> int:
        total_duration = step.get("total_duration")
        if isinstance(total_duration, int):
            return total_duration

        think_duration = step.get("think_duration")
        act_duration = step.get("act_duration")
        resolved_think = think_duration if isinstance(think_duration, int) else 0
        resolved_act = act_duration if isinstance(act_duration, int) else 0
        return resolved_think + resolved_act


class CompactChainRenderer:
    """Compact single-line renderer for agent execution."""

    def __init__(self, console: Optional[Console] = None) -> None:
        self._console = console or Console()
        self._enabled = True
        self._current_step = 0

    def enable(self) -> None:
        """Enable rendering."""
        self._enabled = True

    def disable(self) -> None:
        """Disable rendering."""
        self._enabled = False

    def on_llm_request_start(self) -> None:
        """Handle LLM request start."""

    def start_trace(self, trace_id: Optional[str]) -> None:
        """Start trace."""
        if not self._enabled:
            return
        if not trace_id:
            return
        self._current_step = 0

    def on_runtime_event(
        self,
        event: RuntimeEvent,
        *,
        trace_label: str | None = None,
    ) -> None:
        """Render one canonical runtime event."""
        del trace_label
        if not self._enabled:
            return
        if event.event_type in {
            RuntimeEventType.LLM_THINK_STARTED.value,
            RuntimeEventType.TOOL_CALL_STARTED.value,
        }:
            return
        if event.event_type == RuntimeEventType.LLM_THINK_COMPLETED.value:
            self._on_runtime_think_complete(event)
            return
        if event.event_type == RuntimeEventType.STEP_COMPLETED.value:
            self._on_runtime_step_complete()

    def _on_runtime_think_complete(self, event: RuntimeEvent) -> None:
        action_type = (event.payload or {}).get("action_type")
        tool_calls = _tool_call_names((event.payload or {}).get("tool_calls"))

        if action_type == "tool_call" and tool_calls:
            for _ in tool_calls:
                self._console.print("[yellow]⚡[/yellow]", end="")
        elif action_type == "response":
            self._console.print("[green]💬[/green]", end="")
        elif action_type == "finish":
            self._console.print("[blue]✓[/blue]", end="")

    def _on_runtime_step_complete(self) -> None:
        """Handle step complete."""
        if not self._enabled:
            return
        self._current_step += 1

    def finish_trace(self) -> None:
        """Finish trace."""
        if not self._enabled:
            return
        self._console.print()
