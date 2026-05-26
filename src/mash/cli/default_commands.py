"""Default slash commands for remote CLI shells."""

from __future__ import annotations

import json

from mash.tools.subagent import derive_subagent_session_id

from .commands import Command


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
        ctx.renderer.info(f"Primary agent: {deployment.get('primary_agent_id')}")
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
        ctx.renderer.info(f"Session tokens: {payload.get('session_total_tokens')}")

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
        agents = ctx.client.list_agents()
        if not agents:
            ctx.renderer.warn("No agents available.")
            return
        rows = []
        for agent in agents:
            rows.append([str(agent.get("agent_id") or ""), str(agent.get("role") or "")])
        ctx.renderer.table(["Agent", "Role"], rows)

    def use_command(ctx, args: list[str]) -> None:
        if not args:
            ctx.renderer.error("Usage: /use <agent_id>")
            return
        target_agent_id = args[0].strip()
        if not target_agent_id:
            ctx.renderer.error("Usage: /use <agent_id>")
            return

        agents = ctx.client.list_agents()
        roles = {
            str(agent.get("agent_id") or "").strip(): str(agent.get("role") or "").strip()
            for agent in agents
            if str(agent.get("agent_id") or "").strip()
        }
        current_agent_id = ctx.agent_id
        current_role = roles.get(current_agent_id)
        target_role = roles.get(target_agent_id)

        next_session_id = ctx.session_ids.get(target_agent_id)
        if (
            next_session_id is None
            and current_role == "primary"
            and target_role == "subagent"
        ):
            next_session_id = derive_subagent_session_id(
                current_agent_id,
                ctx.session_id,
                target_agent_id,
            )
        if next_session_id is None:
            next_session_id = ctx.session_id

        ctx.agent_id = target_agent_id
        ctx.session_id = next_session_id
        ctx.session_ids[target_agent_id] = next_session_id
        ctx.renderer.info(f"Switched to agent: {ctx.agent_id}")

    def workflow_command(ctx, args: list[str]) -> None:
        if not args:
            ctx.renderer.error("Usage: /workflow [list|run|status] ...")
            return
        subcommand = args[0].strip().lower()
        if subcommand == "list":
            workflows = ctx.client.list_workflows()
            if not workflows:
                ctx.renderer.warn("No workflows registered.")
                return
            rows = []
            for workflow in workflows:
                tasks = workflow.get("tasks")
                rendered_tasks = []
                if isinstance(tasks, list):
                    for task in tasks:
                        if not isinstance(task, dict):
                            continue
                        task_id = str(task.get("task_id") or "")
                        agent_id = str(task.get("agent_id") or "")
                        rendered_tasks.append(f"{task_id} -> {agent_id}")
                rows.append(
                    [
                        str(workflow.get("workflow_id") or ""),
                        ", ".join(rendered_tasks),
                    ]
                )
            ctx.renderer.table(["Workflow ID", "Tasks"], rows)
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

            streamed_response_text: dict[str, str] = {}
            try:
                for event in ctx.client.stream_workflow_run(workflow_id, run_id):
                    event_name = str(event.get("event") or "")
                    payload = event.get("data")
                    if not isinstance(payload, dict):
                        continue

                    task_id = str(payload.get("task_id") or "")
                    task_agent_id = str(payload.get("task_agent_id") or "")
                    task_label = f"Workflow task {task_id}" if task_id else "Workflow task"

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
                        if task_agent_id:
                            streamed_text = shell.extract_streamed_response_text(
                                payload,
                                agent_id=task_agent_id,
                            )
                            if streamed_text:
                                streamed_response_text[task_id] = streamed_text
                                ctx.renderer.markdown(streamed_text)
                        continue

                    if event_name == "request.completed":
                        response_payload = payload.get("response")
                        if isinstance(response_payload, dict):
                            text = str(response_payload.get("text") or "")
                        else:
                            text = str(payload.get("text") or "")
                        if text and text != streamed_response_text.get(task_id):
                            ctx.renderer.markdown(text)
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
            output = run.get("output")
            if isinstance(output, dict):
                ctx.renderer.print(json.dumps(output, ensure_ascii=True, indent=2))
            return

        ctx.renderer.error("Usage: /workflow [list|run|status] ...")

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
        Command(name="agents", help="List available agents", handler=agents_command)
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
        Command(name="use", help="Switch to a different agent", handler=use_command)
    )
    shell.command_registry.register(
        Command(
            name="workflow",
            help="List, run, and inspect workflows",
            handler=workflow_command,
        )
    )
