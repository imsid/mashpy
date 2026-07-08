"""Default slash commands for remote CLI shells."""

from __future__ import annotations

import json

from .commands import Command


def _host_members(host: dict) -> list[tuple[dict, str]]:
    members: list[tuple[dict, str]] = []
    primary = host.get("primary")
    if isinstance(primary, dict):
        members.append((primary, "primary"))
    subagents = host.get("subagents")
    if isinstance(subagents, list):
        for member in subagents:
            if isinstance(member, dict):
                members.append((member, "subagent"))
    return members


def _host_workflow_ids(host: dict) -> set[str]:
    workflows = host.get("workflows")
    if not isinstance(workflows, list):
        return set()
    return {str(workflow_id) for workflow_id in workflows}


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

    _p = lambda text: renderer.print(f"{prefix}{text}")  # noqa: E731

    _p(
        f"  {_fmt_status(status)}  {_fmt_ms(total_ms)}"
        f"  |  {counts.get('step_count', 0)} steps"
        f"  {counts.get('tool_call_count', 0)} tool calls"
        f"  {total_tokens} tokens"
    )
    _p("")

    segments = [
        ("subagent_call", timing.get("total_subagent_ms", 0), timing.get("pct_subagent", 0)),
        ("think", timing.get("total_think_ms", 0), timing.get("pct_think", 0)),
        ("tool_call", timing.get("total_tool_ms", 0), timing.get("pct_tool", 0)),
        ("cold_start", timing.get("cold_start_ms", 0), timing.get("pct_cold_start", 0)),
        ("context_load", timing.get("context_load_ms", 0), 0),
        ("idle", timing.get("idle_ms", 0), 0),
    ]
    visible = [(label, ms, pct) for label, ms, pct in segments if ms > 0]
    if visible:
        _p("  [bold]Spans by time[/bold]")
        _p("  " + "─" * 50)
        label_width = max(len(label) for label, _, _ in visible)
        dur_width = max(len(_fmt_ms(ms)) for _, ms, _ in visible)
        for label, ms, pct in visible:
            bar = "█" * int(pct / 2.5) if pct > 0 else ""
            pct_str = f"{pct:5.1f}%" if pct else ""
            _p(f"  {label:<{label_width}}  {_fmt_ms(ms):>{dur_width}}  {pct_str}  {bar}")
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
            _p(f"  {kind:<{kind_width}}  {name:<{name_width}}  {dur:>{dur_width}}  {step}")
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
            _p(f"  ┌─ subagent_call: {agent_id} " + "─" * max(0, 32 - len(agent_id)))
            _render_trace(renderer, child_data, depth=depth + 1)
            renderer.print(f"{'│  ' * depth}  └" + "─" * 51)
        else:
            dur = _fmt_ms(sub.get("duration_ms", 0))
            _p(f"  subagent_call: {agent_id}  {dur}  (trace data unavailable)")


def register_default_commands(shell) -> None:
    """Register built-in commands for a remote CLI shell."""

    def help_command(ctx, _args: list[str]) -> None:
        commands = shell.command_registry.list_commands()
        if not commands:
            ctx.renderer.info("No commands available.")
            return

        ctx.renderer.info("Available commands:")
        for command in commands:
            aliases = f" (aliases: {', '.join(command.aliases)})" if command.aliases else ""
            ctx.renderer.print(f"  /{command.name}{aliases} - {command.help}")

    def exit_command(_ctx, _args: list[str]) -> None:
        raise SystemExit(0)

    def clear_command(ctx, _args: list[str]) -> None:
        ctx.renderer.clear()

    def status_command(ctx, _args: list[str]) -> None:
        health = ctx.client.health()
        deployment = health.get("deployment") or {}
        ctx.renderer.info(f"Deployment: {ctx.api_base_url}")
        ctx.renderer.info(f"Agents: {len(deployment.get('agents') or [])}")
        ctx.renderer.info(f"Hosts: {len(deployment.get('hosts') or [])}")
        ctx.renderer.info(f"Current host: {ctx.host_id or '(none)'}")
        ctx.renderer.info(f"Current agent: {ctx.agent_id}")
        ctx.renderer.info(f"Session ID: {ctx.session_id}")

    def history_command(ctx, args: list[str]) -> None:
        limit = None
        if args:
            try:
                limit = int(args[0])
            except ValueError:
                ctx.renderer.error("Limit must be a number")
                return

        turns = ctx.client.get_history(ctx.agent_id, ctx.session_id, limit=limit)
        if not turns:
            ctx.renderer.warn("No conversation history.")
            return

        ctx.renderer.info(f"Conversation history ({len(turns)} turns):")
        for index, turn in enumerate(turns, 1):
            ctx.renderer.print(f"\n--- Turn {index} ---")
            ctx.renderer.print(f"User: {turn['user_message']}")
            ctx.renderer.print(f"Agent: {turn['agent_response']}")

    def session_command(ctx, _args: list[str]) -> None:
        payload = ctx.client.get_session(ctx.agent_id, ctx.session_id)
        ctx.renderer.info(f"Deployment: {ctx.api_base_url}")
        ctx.renderer.info(f"Agent: {payload.get('agent_id') or ctx.agent_id}")
        ctx.renderer.info(f"Session ID: {payload.get('session_id') or ctx.session_id}")
        ctx.renderer.info(f"Model: {payload.get('model')}")
        ctx.renderer.info(f"Max steps: {payload.get('max_steps')}")
        total_tokens = payload.get("total_tokens")
        if total_tokens is None:
            total_tokens = payload.get("session_total_tokens")
        ctx.renderer.info(f"Session tokens: {total_tokens}")
        cache_read = payload.get("cache_read_tokens") or 0
        cache_write = payload.get("cache_write_tokens") or 0
        if cache_read or cache_write:
            ctx.renderer.info(f"  Cache: {cache_read} read / {cache_write} written")

    def sessions_command(ctx, _args: list[str]) -> None:
        sessions = ctx.client.list_sessions(ctx.agent_id)
        if not sessions:
            ctx.renderer.warn("No sessions found.")
            return
        rows = []
        for session in sessions:
            rows.append(
                [
                    str(session.get("session_id") or ""),
                    str(session.get("turn_count") or 0),
                    str(session.get("session_total_tokens") or 0),
                ]
            )
        ctx.renderer.table(["Session ID", "Turns", "Tokens"], rows)

    def agents_command(ctx, _args: list[str]) -> None:
        if ctx.host_id:
            host = ctx.client.get_host(ctx.host_id)
            members = _host_members(host)
            if not members:
                ctx.renderer.warn(f"Host '{ctx.host_id}' has no agents.")
                return
            rows = []
            for member, role in members:
                metadata = member.get("metadata") or {}
                rows.append(
                    [
                        str(member.get("agent_id") or ""),
                        str(metadata.get("display_name") or ""),
                        role,
                    ]
                )
            ctx.renderer.table(["Agent", "Name", "Role"], rows)
            return

        agents = ctx.client.list_agents()
        if not agents:
            ctx.renderer.warn("No agents available.")
            return
        rows = []
        for agent in agents:
            metadata = agent.get("metadata") or {}
            rows.append(
                [
                    str(agent.get("agent_id") or ""),
                    str(metadata.get("display_name") or ""),
                ]
            )
        ctx.renderer.table(["Agent", "Name"], rows)

    def hosts_command(ctx, _args: list[str]) -> None:
        hosts = ctx.client.list_hosts()
        if not hosts:
            ctx.renderer.warn("No hosts defined.")
            return
        rows = [
            [
                str(host.get("host_id") or ""),
                str(host.get("primary") or ""),
                ", ".join(host.get("subagents") or []),
                ", ".join(host.get("workflows") or []),
            ]
            for host in hosts
        ]
        ctx.renderer.table(["Host", "Primary", "Subagents", "Workflows"], rows)

    def workflow_command(ctx, args: list[str]) -> None:
        # Bare `/workflow` behaves like `/workflow list`.
        subcommand = args[0].strip().lower() if args else "list"
        if subcommand == "list":
            workflows = ctx.client.list_workflows(host=ctx.host_id)
            if not workflows:
                if ctx.host_id:
                    ctx.renderer.warn(f"No workflows attached to host '{ctx.host_id}'.")
                else:
                    ctx.renderer.warn("No workflows registered.")
                return
            rows = []
            for workflow in workflows:
                rendered_steps = []
                steps = workflow.get("steps")
                if isinstance(steps, list):
                    for step in steps:
                        if not isinstance(step, dict):
                            continue
                        step_id = str(step.get("step_id") or "")
                        kind = str(step.get("kind") or "")
                        rendered_steps.append(f"{step_id} ({kind})")
                else:
                    tasks = workflow.get("tasks")
                    if isinstance(tasks, list):
                        for task in tasks:
                            if not isinstance(task, dict):
                                continue
                            task_id = str(task.get("task_id") or "")
                            agent_id = str(task.get("agent_id") or "")
                            rendered_steps.append(f"{task_id} -> {agent_id}")
                rows.append(
                    [
                        str(workflow.get("workflow_id") or ""),
                        ", ".join(rendered_steps),
                    ]
                )
            ctx.renderer.table(["Workflow ID", "Steps"], rows)
            return

        if subcommand == "run":
            if len(args) < 2:
                ctx.renderer.error("Usage: /workflow run <workflow_id> [dedup_key] [--input JSON_OBJECT]")
                return
            workflow_id = args[1].strip()
            dedup_key = None
            workflow_input = None
            remaining = list(args[2:])
            input_index = None
            for index, value in enumerate(remaining):
                if value == "--input":
                    input_index = index
                    break
            if input_index is not None:
                if input_index + 1 >= len(remaining):
                    ctx.renderer.error("Usage: /workflow run <workflow_id> [dedup_key] [--input JSON_OBJECT]")
                    return
                raw_input = " ".join(remaining[input_index + 1 :]).strip()
                if (
                    len(raw_input) >= 2
                    and raw_input[0] == raw_input[-1]
                    and raw_input[0] in {"'", '"'}
                ):
                    raw_input = raw_input[1:-1]
                remaining = remaining[:input_index]
                try:
                    decoded_input = json.loads(raw_input)
                except json.JSONDecodeError as exc:
                    ctx.renderer.error(f"Workflow input must be valid JSON: {exc.msg}")
                    return
                if not isinstance(decoded_input, dict):
                    ctx.renderer.error("Workflow input must be a JSON object")
                    return
                workflow_input = decoded_input
            if len(remaining) > 1:
                ctx.renderer.error("Usage: /workflow run <workflow_id> [dedup_key] [--input JSON_OBJECT]")
                return
            if remaining:
                dedup_key = remaining[0].strip() or None
            if not workflow_id:
                ctx.renderer.error("Usage: /workflow run <workflow_id> [dedup_key] [--input JSON_OBJECT]")
                return
            if ctx.host_id:
                attached = _host_workflow_ids(ctx.client.get_host(ctx.host_id))
                if workflow_id not in attached:
                    ctx.renderer.error(
                        f"Workflow '{workflow_id}' is not attached to host "
                        f"'{ctx.host_id}'. Use /workflow list to see attached workflows."
                    )
                    return
            run = ctx.client.run_workflow(
                workflow_id,
                dedup_key=dedup_key,
                workflow_input=workflow_input,
            )
            ctx.renderer.info(f"Workflow: {run.get('workflow_id') or workflow_id}")
            run_id = str(run.get("run_id") or "")
            ctx.renderer.info(f"Run ID: {run_id}")
            if not run_id:
                ctx.renderer.info(f"Status: {run.get('status') or ''}")
                return

            try:
                for event in ctx.client.stream_workflow_run(workflow_id, run_id):
                    event_name = str(event.get("event") or "")
                    payload = event.get("data")
                    if not isinstance(payload, dict):
                        continue

                    task_id = str(payload.get("task_id") or "")
                    task_agent_id = str(payload.get("task_agent_id") or "")
                    task_label = f"Workflow task {task_id}" if task_id else "Workflow task"

                    if event_name == "request.interaction.create":
                        shell._handle_interaction(
                            ctx,
                            str(payload.get("request_id") or ""),
                            payload,
                            agent_id=task_agent_id or ctx.agent_id,
                        )
                        continue

                    if event_name == "request.interaction.ack":
                        shell._render_interaction_ack(payload)
                        continue

                    if event_name == "workflow.status":
                        status = str(payload.get("status") or "")
                        if status:
                            ctx.renderer.info(f"Workflow status: {status}")
                        continue

                    if event_name == "workflow.task.started":
                        ctx.renderer.info(f"{task_label} started")
                        continue

                    if event_name == "workflow.task.completed":
                        ctx.renderer.info(f"{task_label} completed")
                        continue

                    if event_name == "workflow.task.error":
                        ctx.renderer.error(f"{task_label} error")
                        continue

                    if event_name == "agent.trace":
                        shell.render_runtime_trace_payload(
                            payload,
                            trace_label=task_label,
                            agent_id=task_agent_id or None,
                        )
                        continue

                    if event_name == "request.completed":
                        shell.chain_renderer.finish_trace()
                        response_payload = payload.get("response")
                        structured_output = None
                        if isinstance(response_payload, dict):
                            structured_output = response_payload.get("structured_output")
                        if isinstance(structured_output, dict):
                            shell.render_structured_output(
                                workflow_id, task_id, task_agent_id, structured_output
                            )
                        else:
                            chain_streamed = shell.chain_renderer.take_streamed_text()
                            fallback = str(payload.get("text") or "")
                            shell.render_final_response(
                                ctx, response_payload, fallback, chain_streamed
                            )
                        continue

                    if event_name == "request.error":
                        error = payload.get("error")
                        ctx.renderer.error(str(error or "workflow task request failed"))
                        continue

                    if event_name == "workflow.error":
                        error = payload.get("error")
                        ctx.renderer.error(str(error or "workflow stream failed"))
                        return
            finally:
                shell.chain_renderer.finish_trace()
            return

        if subcommand == "status":
            if len(args) < 3:
                ctx.renderer.error("Usage: /workflow status <workflow_id> <run_id>")
                return
            workflow_id = args[1].strip()
            run_id = args[2].strip()
            if not workflow_id or not run_id:
                ctx.renderer.error("Usage: /workflow status <workflow_id> <run_id>")
                return
            if ctx.host_id:
                attached = _host_workflow_ids(ctx.client.get_host(ctx.host_id))
                if workflow_id not in attached:
                    ctx.renderer.error(
                        f"Workflow '{workflow_id}' is not attached to host "
                        f"'{ctx.host_id}'. Use /workflow list to see attached workflows."
                    )
                    return
            run = ctx.client.get_workflow_run(workflow_id, run_id)
            rows = [
                ["run_id", str(run.get("run_id") or "")],
                ["workflow_id", str(run.get("workflow_id") or workflow_id)],
                ["dedup_key", str(run.get("dedup_key") or "")],
                ["status", str(run.get("status") or "")],
                ["created_at", str(run.get("created_at") or "")],
                ["started_at", str(run.get("started_at") or "")],
                ["finished_at", str(run.get("finished_at") or "")],
                ["error", str(run.get("error") or "")],
            ]
            ctx.renderer.table(["Field", "Value"], rows)
            steps = run.get("steps")
            if isinstance(steps, list) and steps:
                step_rows = [
                    [
                        str(step.get("step_id") or ""),
                        str(step.get("kind") or ""),
                        str(step.get("status") or ""),
                    ]
                    for step in steps
                    if isinstance(step, dict)
                ]
                ctx.renderer.table(["Step", "Kind", "Status"], step_rows)
            output = run.get("output")
            if isinstance(output, dict):
                ctx.renderer.print(json.dumps(output, ensure_ascii=True, indent=2))
            return

        if subcommand == "resume":
            if len(args) < 3:
                ctx.renderer.error("Usage: /workflow resume <workflow_id> <run_id>")
                return
            workflow_id = args[1].strip()
            run_id = args[2].strip()
            if not workflow_id or not run_id:
                ctx.renderer.error("Usage: /workflow resume <workflow_id> <run_id>")
                return
            if ctx.host_id:
                attached = _host_workflow_ids(ctx.client.get_host(ctx.host_id))
                if workflow_id not in attached:
                    ctx.renderer.error(
                        f"Workflow '{workflow_id}' is not attached to host "
                        f"'{ctx.host_id}'. Use /workflow list to see attached workflows."
                    )
                    return
            run = ctx.client.resume_workflow_run(workflow_id, run_id)
            ctx.renderer.info(
                f"Resumed run {run.get('run_id') or run_id} "
                f"(status {run.get('status') or 'unknown'})"
            )
            return

        ctx.renderer.error("Usage: /workflow [list|run|status|resume] ...")

    def feedback_command(ctx, args: list[str]) -> None:
        message = " ".join(args).strip()
        if not message:
            ctx.renderer.error("Usage: /feedback <message>")
            return
        try:
            ctx.client.submit_feedback(
                ctx.agent_id,
                message=message,
                host_id=ctx.host_id,
                session_id=ctx.session_id,
                request_id=ctx.last_request_id,
            )
        except Exception as exc:
            ctx.renderer.error(f"Failed to record feedback: {exc}")
            return
        request_note = f", request {ctx.last_request_id}" if ctx.last_request_id else ""
        ctx.renderer.info(f"✓ Feedback recorded (session {ctx.session_id}{request_note})")

    def trace_command(ctx, args: list[str]) -> None:
        count = 1
        if args:
            try:
                count = int(args[0])
            except ValueError:
                ctx.renderer.error("Usage: /trace [N]  (N = number of recent traces, default 1)")
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
                data = ctx.client.get_trace_analysis(ctx.agent_id, ctx.session_id, trace_id)
            except Exception as exc:
                ctx.renderer.error(f"Failed to load analysis for trace {trace_id}: {exc}")
                continue

            ctx.renderer.info(f"Trace {trace_id}")
            _render_trace(ctx.renderer, data)

    shell.command_registry.register(
        Command(name="help", help="Show available commands", handler=help_command, aliases=("h", "?"))
    )
    shell.command_registry.register(
        Command(name="exit", help="Exit the application", handler=exit_command, aliases=("quit", "q"))
    )
    shell.command_registry.register(
        Command(name="clear", help="Clear the screen", handler=clear_command, aliases=("cls",))
    )
    shell.command_registry.register(
        Command(name="status", help="Show deployment status", handler=status_command)
    )
    shell.command_registry.register(
        Command(
            name="agent",
            help="List available agents (host members only when connected through a host)",
            handler=agents_command,
        )
    )
    shell.command_registry.register(
        Command(name="session", help="Show current remote session info", handler=session_command)
    )
    shell.command_registry.register(
        Command(name="sessions", help="List remote sessions for the current agent", handler=sessions_command)
    )
    shell.command_registry.register(
        Command(name="history", help="View conversation history", handler=history_command)
    )
    shell.command_registry.register(
        Command(
            name="host",
            help="List defined hosts (retarget with `mash compose` or `mash connect`)",
            handler=hosts_command,
        )
    )
    shell.command_registry.register(
        Command(
            name="workflow",
            help="List, run, inspect, and resume workflows; bare `/workflow` lists (host-attached only when connected through a host)",
            handler=workflow_command,
        )
    )
    shell.command_registry.register(
        Command(
            name="trace",
            help="Show trace analysis for recent traces (/trace [N])",
            handler=trace_command,
        )
    )
    shell.command_registry.register(
        Command(
            name="feedback",
            help="Send feedback or a bug report about this session (/feedback <message>)",
            handler=feedback_command,
        )
    )
