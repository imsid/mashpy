"""Real-time chain of thought renderer for agent execution."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.live import Live
from rich.text import Text



class ChainOfThoughtRenderer:
    """Renders agent's chain of thought in real-time."""

    def __init__(self, console: Optional[Console] = None) -> None:
        """Initialize renderer.

        Args:
            console: Rich console instance. Creates new one if not provided.
        """
        self._console = console or Console()
        self._current_trace_id: Optional[str] = None
        self._current_trace_label: Optional[str] = None
        self._current_step: int = 0
        self._steps: List[Dict[str, Any]] = []
        self._live: Optional[Live] = None
        self._enabled = True

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
        title = "Agent Execution Started"
        if label:
            title = f"{label} Execution Started"
        self._console.print(f"\n[bold cyan]{title}[/bold cyan]")

    def on_think_complete(self, event: Any) -> None:
        """Handle think complete event.

        Args:
            event: Agent trace event.
        """
        if not self._enabled:
            return

        # Start new trace if needed
        if event.trace_id != self._current_trace_id:
            trace_label = None
            if hasattr(event, "payload") and event.payload:
                trace_label = event.payload.get("trace_label")
            self.start_trace(event.trace_id, label=trace_label)

        # Extract tool_calls_detail from payload if available
        tool_calls_detail = None
        assistant_text = None
        if hasattr(event, "payload") and event.payload:
            tool_calls_detail = event.payload.get("tool_calls_detail")
            assistant_text = event.payload.get("assistant_text")

        step_info = {
            "step": event.step_id,
            "action_type": event.action_type,
            "tool_calls": event.tool_calls,
            "tool_calls_detail": tool_calls_detail,
            "assistant_text": assistant_text,
            "token_usage": event.token_usage,
            "think_duration": event.duration_ms,
        }
        if isinstance(event.step_id, int) and event.step_id >= 0:
            step_info["display_step"] = event.step_id + 1
        self._steps.append(step_info)

        # Render thinking
        self._render_think(step_info)

    def on_act_complete(self, event: Any) -> None:
        """Handle act complete event.

        Args:
            event: Agent trace event.
        """
        if not self._enabled or not self._steps:
            return

        # Update last step with act duration
        self._steps[-1]["act_duration"] = event.duration_ms
        self._render_act(self._steps[-1])

    def on_step_complete(self, event: Any) -> None:
        """Handle step complete event.

        Args:
            event: Agent trace event.
        """
        if not self._enabled or not self._steps:
            return

        # Update last step with total duration
        self._steps[-1]["total_duration"] = event.duration_ms
        self._render_step_complete(self._steps[-1])
        if isinstance(event.step_id, int) and event.step_id >= 0:
            self._current_step = max(self._current_step, event.step_id + 1)
        else:
            self._current_step += 1

    def on_llm_request_start(self) -> None:
        """Handle LLM request start."""
        if not self._enabled:
            return
        # Could show a spinner here if desired

    def on_llm_request_complete(self, event: Any) -> None:
        """Handle LLM request complete.

        Args:
            event: LLM event.
        """
        # Events are already captured in think_complete via token_usage

    def finish_trace(self) -> None:
        """Finish the current trace."""
        if not self._enabled:
            return
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

        if assistant_text:
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
        """Render step completion.

        Args:
            step: Step information.
        """
        # Just a blank line for spacing
        # self._console.print()

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
        """Initialize compact renderer.

        Args:
            console: Rich console instance.
        """
        self._console = console or Console()
        self._enabled = True
        self._current_step = 0

    def enable(self) -> None:
        """Enable rendering."""
        self._enabled = True

    def disable(self) -> None:
        """Disable rendering."""
        self._enabled = False

    def start_trace(self, trace_id: Optional[str]) -> None:
        """Start trace."""
        if not self._enabled:
            return
        if not trace_id:
            return
        self._current_step = 0
        self._console.print("[dim]Thinking...[/dim]", end=" ")

    def on_think_complete(self, event: Any) -> None:
        """Handle think complete."""
        if not self._enabled:
            return

        action_type = event.action_type
        tool_calls = event.tool_calls or []

        if action_type == "tool_call" and tool_calls:
            # Show tool icons
            for _ in tool_calls:
                self._console.print("[yellow]⚡[/yellow]", end="")
        elif action_type == "response":
            self._console.print("[green]💬[/green]", end="")
        elif action_type == "finish":
            self._console.print("[blue]✓[/blue]", end="")

    def on_act_complete(self) -> None:
        """Handle act complete."""
        # Compact mode doesn't show act separately

    def on_step_complete(self) -> None:
        """Handle step complete."""
        if not self._enabled:
            return
        self._current_step += 1

    def finish_trace(self) -> None:
        """Finish trace."""
        if not self._enabled:
            return
        self._console.print()  # New line after all steps
