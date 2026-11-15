"""Requests slash commands."""

from __future__ import annotations

from ..background import parse_bg_args
from ..commands import Command
from .groups import GROUP_REQUESTS


def _fmt_ms(ms: float) -> str:
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{int(ms)}ms"


def _fmt_status(status: str) -> str:
    if status == "completed":
        return "✓"
    if status == "error":
        return "✗"
    return status


def _render_trace(renderer, data: dict, *, depth: int = 0) -> None:
    prefix = "│  " * depth
    analysis = data.get("analysis") or {}
    timing = analysis.get("timing") or {}
    tokens = data.get("tokens") or {}
    counts = data.get("counts") or {}

    if not counts:
        counts = {
            "step_count": analysis.get("step_count", 0),
            "tool_call_count": analysis.get("tool_call_count", 0),
        }
    if not tokens:
        tokens = {
            "input_tokens": analysis.get("input_tokens", 0),
            "output_tokens": analysis.get("output_tokens", 0),
        }

    total_ms = timing.get("total_duration_ms", 0)
    status = data.get("status", "unknown")
    total_tokens = tokens.get("input_tokens", 0) + tokens.get("output_tokens", 0)

    def _p(text: str) -> None:
        renderer.print(f"{prefix}{text}")

    _p(
        f"  {_fmt_status(status)}  {_fmt_ms(total_ms)}"
        f"  |  {counts.get('step_count', 0)} steps"
        f"  {counts.get('tool_call_count', 0)} tool calls"
        f"  {total_tokens} tokens"
    )
    _p("")

    segments = [
        (
            "execute_tool RunSubAgents",
            timing.get("total_subagent_ms", 0),
            timing.get("pct_subagent", 0),
        ),
        ("chat", timing.get("total_think_ms", 0), timing.get("pct_think", 0)),
        ("execute_tool", timing.get("total_tool_ms", 0), timing.get("pct_tool", 0)),
        (
            "mash.cold_start",
            timing.get("cold_start_ms", 0),
            timing.get("pct_cold_start", 0),
        ),
        ("mash.context_load", timing.get("context_load_ms", 0), 0),
        ("idle", timing.get("idle_ms", 0), 0),
    ]
    visible = [(label, ms, pct) for label, ms, pct in segments if ms > 0]
    if visible:
        _p("  [bold]Spans by time[/bold]")
        _p("  " + "─" * 50)
        label_width = max(len(label) for label, _, _ in visible)
        dur_width = max(len(_fmt_ms(ms)) for _, ms, _ in visible)
        for label, ms, pct in visible:
            pct_bar = "█" * int(pct / 2.5) if pct > 0 else ""
            pct_str = f"{pct:5.1f}%" if pct else ""
            _p(
                f"  {label:<{label_width}}  {_fmt_ms(ms):>{dur_width}}  {pct_str}  {pct_bar}"
            )
        _p("")

    tool_stats = analysis.get("tool_stats") or []
    if tool_stats:
        _p("  [bold]Tool spans[/bold]")
        _p("  " + "─" * 50)
        name_width = max(len(str(t.get("tool_name", ""))) for t in tool_stats)
        for t in tool_stats:
            name = str(t.get("tool_name", ""))
            count = t.get("count", 0)
            total = _fmt_ms(t.get("total_ms", 0))
            avg = _fmt_ms(t.get("avg_ms", 0))
            call_word = "call" if count == 1 else "calls"
            _p(f"  {name:<{name_width}}  {count} {call_word}  {total} total  {avg} avg")
        _p("")

    slowest = analysis.get("slowest_operations") or []
    if slowest:
        _p("  [bold]Slowest spans[/bold]")
        _p("  " + "─" * 50)
        kind_width = max(len(str(s.get("kind", ""))) for s in slowest[:5])
        name_width = max(len(str(s.get("name", ""))) for s in slowest[:5])
        dur_width = max(len(_fmt_ms(s.get("duration_ms", 0))) for s in slowest[:5])
        for s in slowest[:5]:
            kind = str(s.get("kind", ""))
            name = str(s.get("name", ""))
            dur = _fmt_ms(s.get("duration_ms", 0))
            step = f"step {s['step_index']}" if s.get("step_index") is not None else ""
            _p(
                f"  {kind:<{kind_width}}  {name:<{name_width}}  {dur:>{dur_width}}  {step}"
            )
        _p("")

    subagent_traces = analysis.get("subagent_traces") or []
    for sub in subagent_traces:
        agent_id = sub.get("agent_id", "unknown")
        child = sub.get("child_analysis")
        if child:
            child_data = {
                "analysis": child,
                "status": "completed",
            }
            _p(f"  ┌─ invoke_agent {agent_id} " + "─" * max(0, 32 - len(agent_id)))
            _render_trace(renderer, child_data, depth=depth + 1)
            renderer.print(f"{'│  ' * depth}  └" + "─" * 51)
        else:
            dur = _fmt_ms(sub.get("duration_ms", 0))
            _p(f"  invoke_agent {agent_id}  {dur}  (trace data unavailable)")


def register(shell) -> None:
    def add(
        name: str, help: str, handler, group: str, aliases: tuple[str, ...] = ()
    ) -> None:
        shell.command_registry.register(
            Command(name=name, help=help, handler=handler, aliases=aliases, group=group)
        )

    async def bg_command(ctx, args: list[str]) -> None:
        parsed = parse_bg_args(args)
        if isinstance(parsed, str):
            ctx.renderer.error(parsed)
            return
        if parsed.action == "start":
            await shell.start_background(ctx, parsed.message)
        elif parsed.action == "list":
            await shell.list_background(ctx)
        elif parsed.action == "switch":
            await shell.switch_to(ctx, parsed.request_id)
        else:
            await shell.kill(ctx, parsed.request_id)

    async def subagent_command(ctx, args: list[str]) -> None:
        if not args:
            shell.list_subagents()
        elif len(args) == 1 and args[0] != "cancel":
            await shell.show_subagent(args[0])
        elif len(args) == 2 and args[0] == "cancel":
            await shell.cancel_subagent(args[1])
        else:
            ctx.renderer.error(
                "Usage: /subagent | /subagent <n> | /subagent cancel <n>"
            )

    async def retry_command(ctx, args: list[str]) -> None:
        del ctx, args
        await shell.retry()

    def trace_command(ctx, args: list[str]) -> None:
        count = 1
        if args:
            try:
                count = int(args[0])
            except ValueError:
                ctx.renderer.error(
                    "Usage: /trace [N]  (N = number of recent traces, default 1)"
                )
                return
        if count < 1:
            ctx.renderer.error("N must be at least 1")
            return

        traces = ctx.client.list_traces(ctx.agent_id, ctx.session_id, limit=count)
        if not traces:
            ctx.renderer.warn("No traces found for this session.")
            return

        for i, trace_summary in enumerate(traces):
            trace_id = str(trace_summary.get("trace_id") or "")
            if not trace_id:
                continue

            if i > 0:
                ctx.renderer.print("")

            try:
                data = ctx.client.get_trace_analysis(
                    ctx.agent_id, ctx.session_id, trace_id
                )
            except Exception as exc:
                ctx.renderer.error(
                    f"Failed to load analysis for trace {trace_id}: {exc}"
                )
                continue

            ctx.renderer.info(f"Trace {trace_id}")
            _render_trace(ctx.renderer, data)

    add(
        "bg",
        "Run a message in the background: /bg <message> | list | switch <id> | kill <id>",
        bg_command,
        GROUP_REQUESTS,
    )
    add(
        "subagent",
        "The last request's subagents: list, show one (<n>) or cancel one (cancel <n>)",
        subagent_command,
        GROUP_REQUESTS,
    )
    add("retry", "Run the last request again", retry_command, GROUP_REQUESTS)
    add(
        "trace",
        "Timing and tool statistics of recent requests (/trace [n])",
        trace_command,
        GROUP_REQUESTS,
    )
