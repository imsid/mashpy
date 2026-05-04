"""Default slash commands for remote CLI shells."""

from __future__ import annotations

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
