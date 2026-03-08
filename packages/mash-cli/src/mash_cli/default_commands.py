"""Default slash commands for CLI shells."""

from __future__ import annotations

import json
from .commands import Command


def register_default_commands(shell) -> None:
    """Register built-in commands for a CLI shell."""

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

    def session_command(ctx, _args: list[str]) -> None:
        runtime = ctx.runtime
        ctx.renderer.info(f"App: {ctx.app_id}")
        ctx.renderer.info(f"Session ID: {ctx.session_id}")
        ctx.renderer.info(f"Primary agent: {runtime.app_id}")
        subagent_ids = runtime.get_subagent_ids()
        if subagent_ids:
            ctx.renderer.info(f"Subagents: {', '.join(subagent_ids)}")
        ctx.renderer.info(f"Model: {runtime.get_model()}")
        ctx.renderer.info(f"Max steps: {runtime.get_max_steps()}")
        ctx.renderer.info(
            f"Session tokens: {runtime.get_session_total_tokens(ctx.session_id)}"
        )

    def prefs_command(ctx, args: list[str]) -> None:
        runtime = ctx.runtime
        if not args:
            prefs = runtime.get_latest_preferences()
            if prefs:
                ctx.renderer.info("Current preferences:")
                ctx.renderer.print(json.dumps(prefs, indent=2))
            else:
                ctx.renderer.warn("No preferences set.")
            return

        subcommand = args[0].lower()
        if subcommand == "set":
            if len(args) < 2:
                ctx.renderer.error("Usage: /prefs set <json>")
                return
            try:
                prefs = json.loads(" ".join(args[1:]))
                if not isinstance(prefs, dict):
                    ctx.renderer.error("Preferences must be a JSON object")
                    return
                runtime.set_preferences(ctx.session_id, prefs)
                ctx.renderer.info("Preferences saved successfully.")
            except json.JSONDecodeError as exc:
                ctx.renderer.error(f"Invalid JSON: {exc}")
            return

        if subcommand == "clear":
            runtime.set_preferences(ctx.session_id, {})
            ctx.renderer.info("Preferences cleared.")
            return

        ctx.renderer.error(f"Unknown subcommand: {subcommand}")
        ctx.renderer.info("Usage: /prefs [set <json> | clear]")

    def app_data_command(ctx, args: list[str]) -> None:
        runtime = ctx.runtime
        if not args:
            args = ["list"]

        subcommand = args[0].lower()
        if subcommand == "list":
            data = runtime.list_app_data(ctx.session_id)
            if data:
                ctx.renderer.info(f"App data ({len(data)} entries):")
                for entry in data:
                    ctx.renderer.print(f"  {entry['key']}: {json.dumps(entry['value'])}")
            else:
                ctx.renderer.warn("No app data stored.")
            return

        if subcommand == "get":
            if len(args) < 2:
                ctx.renderer.error("Usage: /app_data get <key>")
                return
            key = args[1]
            value = runtime.get_app_data(ctx.session_id, key)
            if value is not None:
                ctx.renderer.info(f"Value for '{key}':")
                ctx.renderer.print(json.dumps(value, indent=2))
            else:
                ctx.renderer.warn(f"No data found for key: {key}")
            return

        if subcommand == "set":
            if len(args) < 3:
                ctx.renderer.error("Usage: /app_data set <key> <json>")
                return
            key = args[1]
            try:
                value = json.loads(" ".join(args[2:]))
                runtime.set_app_data(ctx.session_id, key, value)
                ctx.renderer.info(f"Data stored for key: {key}")
            except json.JSONDecodeError as exc:
                ctx.renderer.error(f"Invalid JSON: {exc}")
            return

        if subcommand == "delete":
            if len(args) < 2:
                ctx.renderer.error("Usage: /app_data delete <key>")
                return
            key = args[1]
            deleted = runtime.delete_app_data(ctx.session_id, key)
            if deleted:
                ctx.renderer.info(f"Data deleted for key: {key}")
            else:
                ctx.renderer.warn(f"No data found for key: {key}")
            return

        ctx.renderer.error(f"Unknown subcommand: {subcommand}")
        ctx.renderer.info(
            "Usage: /app_data [list | get <key> | set <key> <json> | delete <key>]"
        )

    def history_command(ctx, args: list[str]) -> None:
        limit = None
        if args:
            try:
                limit = int(args[0])
            except ValueError:
                ctx.renderer.error("Limit must be a number")
                return

        turns = ctx.runtime.get_history_turns(ctx.session_id, limit=limit)
        if not turns:
            ctx.renderer.warn("No conversation history.")
            return

        ctx.renderer.info(f"Conversation history ({len(turns)} turns):")
        for index, turn in enumerate(turns, 1):
            ctx.renderer.print(f"\n--- Turn {index} ---")
            ctx.renderer.print(f"User: {turn['user_message']}")
            ctx.renderer.print(f"Agent: {turn['agent_response']}")

    def compact_command(ctx, _args: list[str]) -> None:
        summary_text, turn_id = ctx.runtime.compact_session(
            ctx.session_id,
            reason="manual",
            session_total_tokens_reset=0,
        )
        if not summary_text:
            ctx.renderer.warn("No conversation history to compact.")
            return

        ctx.renderer.info(f"Conversation compacted (turn_id={turn_id}).")
        ctx.renderer.markdown(summary_text)

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
        Command(name="session", help="Show current session info", handler=session_command)
    )
    shell.command_registry.register(
        Command(name="prefs", help="View or set user preferences", handler=prefs_command)
    )
    shell.command_registry.register(
        Command(name="app_data", help="Manage app-specific data", handler=app_data_command)
    )
    shell.command_registry.register(
        Command(name="history", help="View conversation history", handler=history_command)
    )
    shell.command_registry.register(
        Command(name="compact", help="Summarize conversation and save a checkpoint", handler=compact_command)
    )
