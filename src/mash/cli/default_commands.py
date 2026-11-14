"""Default slash commands for remote CLI shells."""

from __future__ import annotations

import json

from datetime import datetime

from rich.markup import escape

from .background import parse_bg_args
from .client import MashHostClientError
from .commands import Command, ExitRepl
from .render import clip, fmt_tokens

GROUP_SESSION = "Session"
GROUP_REQUESTS = "Requests"
GROUP_DEPLOYMENT = "Deployment"
GROUP_REPL = "REPL"

KEYS = (
    ("Enter", "Send the message"),
    ("Alt+Enter, Ctrl-J, \\ then Enter", "Add a line"),
    ("Up, Down", "Move between lines, then through history"),
    ("Shift+arrows", "Select; typing replaces the selection"),
    ("Ctrl-W, Ctrl-K, Ctrl-U", "Cut a word, to the end of the line, to its start"),
    ("Ctrl-Y", "Paste what was cut"),
    ("Ctrl-_", "Undo"),
    ("Ctrl-X Ctrl-E", "Edit the message in $EDITOR"),
    ("/", "Open the command menu"),
    ("Ctrl-C", "Clear the input, then cancel the request, then (twice) leave"),
    ("Esc", "Cancel the running request"),
    ("Ctrl-Z", "Send the running request to the background"),
    ("Ctrl-O", "Show or hide model steps and subagent tool calls"),
    ("Ctrl-L", "Clear the screen"),
    ("Ctrl-D", "Leave"),
)


def _host_members(host: dict) -> list[dict]:
    agents = host.get("agents")
    if not isinstance(agents, list):
        return []
    return [member for member in agents if isinstance(member, dict)]


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
        ("execute_tool RunSubAgents", timing.get("total_subagent_ms", 0), timing.get("pct_subagent", 0)),
        ("chat", timing.get("total_think_ms", 0), timing.get("pct_think", 0)),
        ("execute_tool", timing.get("total_tool_ms", 0), timing.get("pct_tool", 0)),
        ("mash.cold_start", timing.get("cold_start_ms", 0), timing.get("pct_cold_start", 0)),
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
            _p(f"  {label:<{label_width}}  {_fmt_ms(ms):>{dur_width}}  {pct_str}  {pct_bar}")
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
            _p(f"  ┌─ invoke_agent {agent_id} " + "─" * max(0, 32 - len(agent_id)))
            _render_trace(renderer, child_data, depth=depth + 1)
            renderer.print(f"{'│  ' * depth}  └" + "─" * 51)
        else:
            dur = _fmt_ms(sub.get("duration_ms", 0))
            _p(f"  invoke_agent {agent_id}  {dur}  (trace data unavailable)")


def register_default_commands(shell) -> None:
    """Register built-in commands for a remote CLI shell."""

    def help_command(ctx, _args: list[str]) -> None:
        groups: dict[str, list[Command]] = {}
        for command in shell.command_registry.list_commands():
            groups.setdefault(command.group or "More", []).append(command)
        builtin = [GROUP_SESSION, GROUP_REQUESTS, GROUP_DEPLOYMENT]
        # An app's own groups come after the built-in ones, the REPL's last.
        order = builtin + sorted(set(groups) - set(builtin) - {GROUP_REPL}) + [GROUP_REPL]
        width = max((len(c.name) for cs in groups.values() for c in cs), default=0)
        first = True
        for group in order:
            if group not in groups:
                continue
            if not first:
                ctx.renderer.print()
            first = False
            ctx.renderer.info(group)
            for command in groups[group]:
                aliases = (
                    f" (also /{', /'.join(command.aliases)})" if command.aliases else ""
                )
                ctx.renderer.print(
                    f"  [accent]/{command.name:<{width}}[/accent]  "
                    f"{escape(command.help)}[muted]{escape(aliases)}[/muted]"
                )

    def exit_command(_ctx, _args: list[str]) -> None:
        raise ExitRepl()

    def clear_command(ctx, _args: list[str]) -> None:
        ctx.renderer.clear()

    def agents_command(ctx, _args: list[str]) -> None:
        """The agents this REPL's requests can reach, as the companion sees them."""
        if ctx.host_id:
            members = _host_members(ctx.client.get_host(ctx.host_id))
            if not members:
                ctx.renderer.warn(f"Host '{ctx.host_id}' has no agents.")
                return
        else:
            agent = ctx.client.get_agent(ctx.agent_id).get("agent")
            members = [agent] if isinstance(agent, dict) else []
        for index, member in enumerate(members):
            metadata = member.get("metadata") or {}
            agent_id = str(member.get("agent_id") or "")
            name = str(metadata.get("display_name") or "")
            if index:
                ctx.renderer.print()
            ctx.renderer.print(
                f"[agent]{escape(agent_id)}[/agent]"
                + (f" [muted]· {escape(name)}[/muted]" if name else "")
            )
            description = str(metadata.get("description") or "")
            if description:
                ctx.renderer.print(f"  {escape(description)}")
            capabilities = [str(item) for item in metadata.get("capabilities") or []]
            if capabilities:
                ctx.renderer.muted(f"  Capabilities: {escape(', '.join(capabilities))}")
            guidance = str(metadata.get("usage_guidance") or "")
            if guidance:
                ctx.renderer.muted(f"  Use for: {escape(guidance)}")

    def _target_agents(ctx) -> set[str]:
        if not ctx.host_id:
            return {ctx.agent_id}
        members = _host_members(ctx.client.get_host(ctx.host_id))
        return {str(member.get("agent_id") or "") for member in members}

    def tools_command(ctx, _args: list[str]) -> None:
        agents = _target_agents(ctx)

        def rows(items: list[dict], key: str) -> list[list[str]]:
            found = []
            for item in items:
                used_by = sorted(set(item.get("agents") or []) & agents)
                described = item.get(key) or {}
                if used_by:
                    found.append(
                        [
                            escape(str(described.get("name") or "")),
                            escape(", ".join(used_by)),
                            escape(clip(str(described.get("description") or ""), 70)),
                        ]
                    )
            return sorted(found)

        tools = rows(ctx.client.list_tools(), "tool")
        skills = rows(ctx.client.list_skills(), "skill")
        if not tools and not skills:
            ctx.renderer.warn("These agents have no tools or skills.")
            return
        if tools:
            ctx.renderer.table(["Tool", "Agents", "Does"], tools)
        if skills:
            ctx.renderer.table(["Skill", "Agents", "Does"], skills)

    def usage_command(ctx, _args: list[str]) -> None:
        session = ctx.client.get_session(ctx.agent_id, ctx.session_id)
        total = session.get("total_tokens")
        if total is None:
            total = session.get("session_total_tokens")
        line = f"This session: {fmt_tokens(int(total or 0))} tokens"
        cache_read = int(session.get("cache_read_tokens") or 0)
        cache_write = int(session.get("cache_write_tokens") or 0)
        if cache_read or cache_write:
            line += (
                f" (cache: {fmt_tokens(cache_read)} read, "
                f"{fmt_tokens(cache_write)} written)"
            )
        ctx.renderer.info(line)
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        buckets = ctx.client.get_usage(ctx.agent_id, from_ts=midnight.timestamp())
        requests = sum(int(bucket.get("request_count") or 0) for bucket in buckets)
        tokens_in = sum(int(bucket.get("input_tokens") or 0) for bucket in buckets)
        tokens_out = sum(int(bucket.get("output_tokens") or 0) for bucket in buckets)
        ctx.renderer.info(
            f"Today, {ctx.agent_id}: {requests} request{'' if requests == 1 else 's'} · "
            f"{fmt_tokens(tokens_in)} in · {fmt_tokens(tokens_out)} out"
        )

    def memory_command(ctx, args: list[str]) -> None:
        usage = "Usage: /memory | /memory add <text> | /memory forget <id>"
        try:
            if not args:
                try:
                    user = ctx.client.get_cortex_user(ctx.user_id)
                except MashHostClientError as exc:
                    if exc.code != "CORTEX_NOT_FOUND":
                        raise
                    ctx.renderer.warn("Nothing is remembered about you yet.")
                    return
                profile = str(user.get("profile") or "").strip()
                if profile:
                    ctx.renderer.markdown(profile)
                entries = ctx.client.list_cortex_entries(ctx.user_id)
                if entries:
                    ctx.renderer.table(
                        ["ID", "Topic", "Remembered", "From"],
                        [
                            [
                                escape(str(entry.get("entry_id") or "")[:8]),
                                escape(str(entry.get("topic") or "")),
                                escape(clip(str(entry.get("content") or ""), 70)),
                                "you" if entry.get("origin") == "app" else "conversations",
                            ]
                            for entry in entries
                        ],
                    )
                elif not profile:
                    ctx.renderer.warn("Nothing is remembered about you yet.")
                return
            if args[0] == "add" and len(args) > 1:
                text = " ".join(args[1:]).strip()
                # "topic: fact" names the topic; otherwise it is a note.
                topic, _, content = text.partition(":")
                if not content.strip() or len(topic) > 60:
                    topic, content = "note", text
                entry = ctx.client.create_cortex_entry(
                    ctx.user_id, topic=topic.strip(), content=content.strip()
                )
                ctx.renderer.info(
                    f"Remembered ({escape(str(entry.get('entry_id') or '')[:8])})."
                )
                return
            if args[0] == "forget" and len(args) == 2:
                entries = ctx.client.list_cortex_entries(ctx.user_id, limit=200)
                matches = [
                    entry
                    for entry in entries
                    if str(entry.get("entry_id") or "").startswith(args[1])
                ]
                if len(matches) != 1:
                    ctx.renderer.error(
                        f"No entry matches '{args[1]}'."
                        if not matches
                        else f"'{args[1]}' matches {len(matches)} entries; type more of the id."
                    )
                    return
                ctx.client.delete_cortex_entry(ctx.user_id, str(matches[0]["entry_id"]))
                ctx.renderer.info(f"Forgot: {escape(clip(str(matches[0].get('content') or ''), 70))}")
                return
            ctx.renderer.error(usage)
        except MashHostClientError as exc:
            # Without Cortex the deployment has no such routes.
            if exc.status_code == 404 and exc.code != "CORTEX_NOT_FOUND":
                ctx.renderer.warn("Cortex is not enabled on this deployment.")
                return
            raise

    def browser_command(ctx, args: list[str]) -> None:
        if not args:
            sites = ctx.client.list_browser_sites(ctx.user_id)
            if not sites:
                ctx.renderer.warn("Your browser is not signed in to any site.")
                return
            ctx.renderer.table(
                ["Site", "Cookies"],
                [
                    [escape(str(site.get("domain") or "")), str(site.get("cookies") or 0)]
                    for site in sites
                ],
            )
            return
        if args[0] == "forget" and len(args) == 2:
            cleared = ctx.client.clear_browser_site(ctx.user_id, args[1])
            count = int(cleared.get("cookies") or 0)
            ctx.renderer.info(
                f"Signed out of {escape(args[1])} "
                f"({count} cookie{'' if count == 1 else 's'} removed)."
            )
            return
        ctx.renderer.error("Usage: /browser | /browser forget <site>")

    def keys_command(ctx, _args: list[str]) -> None:
        ctx.renderer.table(["Key", "Does"], [list(row) for row in KEYS])

    def copy_command(ctx, args: list[str]) -> None:
        if args not in ([], ["code"]):
            ctx.renderer.error("Usage: /copy | /copy code")
            return
        shell.copy_last(code=bool(args))

    async def retry_command(ctx, args: list[str]) -> None:
        del ctx, args
        await shell.retry()

    async def compact_command(ctx, args: list[str]) -> None:
        del ctx, args
        await shell.compact()

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
                steps = workflow.get("step_preview")
                if isinstance(steps, list):
                    for step in steps:
                        if not isinstance(step, dict):
                            continue
                        step_id = str(step.get("step_id") or "")
                        kind = str(step.get("kind") or "")
                        rendered_steps.append(f"{step_id} ({kind})")
                step_count = int(workflow.get("step_count") or 0)
                if step_count > len(rendered_steps):
                    rendered_steps.append(f"+{step_count - len(rendered_steps)} more")
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
                ctx.renderer.error("Usage: /workflow run <workflow_id> [dedup_key] [--no-follow] [--input JSON_OBJECT]")
                return
            workflow_id = args[1].strip()
            dedup_key = None
            workflow_input = None
            remaining = list(args[2:])
            # Before --input, so JSON that holds the word stays input.
            before_input = remaining[: remaining.index("--input")] if "--input" in remaining else remaining
            follow = "--no-follow" not in before_input
            if not follow:
                remaining.remove("--no-follow")
            input_index = None
            for index, value in enumerate(remaining):
                if value == "--input":
                    input_index = index
                    break
            if input_index is not None:
                if input_index + 1 >= len(remaining):
                    ctx.renderer.error("Usage: /workflow run <workflow_id> [dedup_key] [--no-follow] [--input JSON_OBJECT]")
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
                ctx.renderer.error("Usage: /workflow run <workflow_id> [dedup_key] [--no-follow] [--input JSON_OBJECT]")
                return
            if remaining:
                dedup_key = remaining[0].strip() or None
            if not workflow_id:
                ctx.renderer.error("Usage: /workflow run <workflow_id> [dedup_key] [--no-follow] [--input JSON_OBJECT]")
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
                # A workflow of code steps runs without a user.
                user_id=ctx.user_id or None,
            )
            ctx.renderer.info(f"Workflow: {run.get('workflow_id') or workflow_id}")
            run_id = str(run.get("run_id") or "")
            ctx.renderer.info(f"Run ID: {run_id}")
            if not run_id or not follow:
                ctx.renderer.info(f"Status: {run.get('status') or ''}")
                return

            for event in ctx.client.stream_workflow_run(workflow_id, run_id):
                event_name = str(event.get("event") or "")
                payload = event.get("data")
                if not isinstance(payload, dict):
                    continue
                step_id = str(payload.get("step_id") or "")
                step_label = f"Workflow step {step_id}" if step_id else "Workflow step"

                if event_name == "step.started":
                    ctx.renderer.info(f"{step_label} started")
                    continue

                if event_name == "step.completed":
                    ctx.renderer.info(f"{step_label} completed")
                    continue

                if event_name == "step.failed":
                    error = (payload.get("payload") or {}).get("error")
                    ctx.renderer.error(str(error or f"{step_label} failed"))
                    continue

                if event_name == "workflow.completed":
                    ctx.renderer.info("Workflow status: completed")
                    result = payload.get("result")
                    if isinstance(result, dict):
                        shell.render_workflow_result(workflow_id, result)
                    continue

                if event_name == "workflow.error":
                    error = payload.get("error")
                    ctx.renderer.error(str(error or "workflow stream failed"))
                    return
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
            result = run.get("result")
            if isinstance(result, dict):
                ctx.renderer.print(json.dumps(result, ensure_ascii=True, indent=2))
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
            ctx.renderer.error("Usage: /subagent | /subagent <n> | /subagent cancel <n>")

    async def new_command(ctx, args: list[str]) -> None:
        del ctx, args
        await shell.switch_session(shell.new_session_id(), resumed=False)

    async def resume_command(ctx, args: list[str]) -> None:
        del ctx, args
        await shell.pick_session()

    def add(name: str, help: str, handler, group: str, aliases: tuple[str, ...] = ()) -> None:
        shell.command_registry.register(
            Command(name=name, help=help, handler=handler, aliases=aliases, group=group)
        )

    add("new", "Start a new session", new_command, GROUP_SESSION)
    add("resume", "Go on in one of your earlier sessions", resume_command, GROUP_SESSION)
    add("compact", "Summarize the session so far to free up context", compact_command, GROUP_SESSION)
    add("usage", "Tokens used by this session and today", usage_command, GROUP_SESSION)
    add("copy", "Copy the last answer, or its last code block (/copy code)", copy_command, GROUP_SESSION)

    add("bg", "Run a message in the background: /bg <message> | list | switch <id> | kill <id>", bg_command, GROUP_REQUESTS)
    add("subagent", "The last request's subagents: list, show one (<n>) or cancel one (cancel <n>)", subagent_command, GROUP_REQUESTS)
    add("retry", "Run the last request again", retry_command, GROUP_REQUESTS)
    add("trace", "Timing and tool statistics of recent requests (/trace [n])", trace_command, GROUP_REQUESTS)

    add("agents", "The agents your requests can reach and what each is for", agents_command, GROUP_DEPLOYMENT)
    add("tools", "The tools and skills of those agents", tools_command, GROUP_DEPLOYMENT)
    add("workflow", "List, run, inspect and resume workflows", workflow_command, GROUP_DEPLOYMENT)
    add("memory", "What is remembered about you: /memory | add <text> | forget <id>", memory_command, GROUP_DEPLOYMENT)
    add("browser", "Sites your browser is signed in to: /browser | forget <site>", browser_command, GROUP_DEPLOYMENT)

    add("help", "Show the commands", help_command, GROUP_REPL, ("h", "?"))
    add("keys", "Show the keys", keys_command, GROUP_REPL)
    add("feedback", "Send a note or bug report about this session", feedback_command, GROUP_REPL)
    add("clear", "Clear the screen", clear_command, GROUP_REPL, ("cls",))
    add("exit", "Leave", exit_command, GROUP_REPL, ("quit", "q"))
