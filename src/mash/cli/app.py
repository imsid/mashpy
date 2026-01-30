"""Base application class for building CLI agents."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

from ..core.agent import Agent
from ..core.context import Context, MessageRole
from ..logging import AgentTraceEvent, EventLogger
from ..memory.compaction import compact_conversation
from ..memory.store import ConversationStore
from ..tools.runtime import RUNTIME_TOOLS_SYSTEM_PROMPT, RuntimeToolBuilder
from .chain_renderer import ChainOfThoughtRenderer
from .commands import Command, CommandRegistry
from .render import RichRenderer
from .repl import REPL


@dataclass
class CLIContext:
    """Context for CLI operations."""

    app_name: str
    session_id: str
    renderer: RichRenderer
    cached_files: List[str] = field(default_factory=list)
    agent: Optional[Agent] = None
    store: Optional[ConversationStore] = None


class MashApp:
    """Base class for building agent-powered CLI applications."""

    def __init__(
        self,
        app_name: str,
        agent: Agent,
        store: ConversationStore,
        log_destination: Union[str, Path],
        enable_runtime_tools: bool = True,
    ) -> None:
        """Initialize the application.

        Args:
            app_name: Application name.
            agent: Agent instance.
            store: Conversation store.
            log_destination: Path to log file. If None, uses default location.
        """
        self.app_name = app_name
        self.agent = agent
        self.store = store
        self.session_id = str(uuid.uuid4())

        # Set up event logger
        if log_destination is None:
            log_dir = Path.home() / ".mash" / "logs"
            log_destination = log_dir / f"{self._get_app_slug()}.jsonl"
        self.event_logger = EventLogger(log_destination)

        # Pass logger to agent and LLM
        self.agent.set_event_logger(self.event_logger, self.session_id)
        if hasattr(self.agent.llm, "set_event_logger"):
            self.agent.llm.set_event_logger(
                self.event_logger, self.session_id, self.agent.config.app_id
            )

        # Initialize components
        self.renderer = RichRenderer()
        self.chain_renderer = ChainOfThoughtRenderer(console=self.renderer.console)
        self.agent.set_chain_renderer(self.chain_renderer)

        self.commands = CommandRegistry(
            event_logger=self.event_logger,
            session_id=self.session_id,
            app_id=agent.config.app_id,
        )
        self.context = CLIContext(
            app_name=app_name,
            session_id=self.session_id,
            renderer=self.renderer,
            agent=agent,
            store=store,
        )

        # Register default commands
        self._register_default_commands()

        # Auto-register runtime tools if enabled
        if enable_runtime_tools:
            self._register_runtime_tools()

        # Allow subclasses to register their commands
        self.register_commands()

    def _get_app_slug(self) -> str:
        """Generate a filesystem-safe slug from app name."""
        slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in self.app_name)
        return slug.strip("_") or "mash"

    def _register_runtime_tools(self) -> None:
        """Register runtime tools for memory and preferences."""

        builder = RuntimeToolBuilder(
            store=self.store,
            app_id=self.agent.config.app_id,
            session_id=self.session_id,
        )

        # Register tools with agent
        for tool in builder.build_tools():
            self.agent.tools.register(tool)

        # Append runtime tools prompt to system prompt
        system_prompt = self.agent.config.system_prompt
        if isinstance(system_prompt, list):
            insert_at = len(system_prompt)
            for idx, block in enumerate(system_prompt):
                if isinstance(block, dict) and block.get("cache_control"):
                    insert_at = idx
                    break
            runtime_block = {
                "type": "text",
                "text": f"\n\n{RUNTIME_TOOLS_SYSTEM_PROMPT}",
            }
            system_prompt.insert(insert_at, runtime_block)
            self.agent.config.system_prompt = system_prompt
        else:
            self.agent.config.system_prompt = (
                f"{self.agent.config.system_prompt}\n\n{RUNTIME_TOOLS_SYSTEM_PROMPT}"
            )

    def register_commands(self) -> None:
        """Register application-specific commands.

        Override this in subclasses to add custom commands.
        """

    def register_command(self, command: Command) -> None:
        """Register a command.

        Args:
            command: Command to register.
        """
        self.commands.register(command)

    def run(self) -> None:
        """Run the interactive application."""
        # Setup REPL
        repl = REPL(
            app_name=self.app_name,
            command_registry=self.commands,
            message_handler=self._handle_message,
        )

        # Start REPL
        try:
            repl.run(self.context)
        except KeyboardInterrupt:
            self.renderer.warn("\nBye.")
        except SystemExit:
            pass

    def _handle_message(self, ctx: CLIContext, message: str) -> None:
        """Handle user message.

        Args:
            ctx: CLI context.
            message: User message.
        """
        if ctx.store and self.agent.config.compaction_token_threshold > 0:
            session_total_tokens = self._get_session_total_tokens(ctx)
            if session_total_tokens >= self.agent.config.compaction_token_threshold:
                summary_text, summary_turn_id = compact_conversation(
                    store=ctx.store,
                    llm=self.agent.llm,
                    app_id=self.agent.config.app_id,
                    session_id=ctx.session_id,
                    model=self.agent.config.model,
                    max_tokens=self.agent.config.max_tokens,
                    temperature=self.agent.config.compaction_temperature,
                    turn_limit=self.agent.config.compaction_turn_limit,
                    reason="auto",
                    session_total_tokens_reset=0,
                )
                if summary_text:
                    ctx.renderer.info(
                        "Compaction triggered — summary checkpoint created."
                    )
                    ctx.renderer.markdown(summary_text)
                if summary_text and self.event_logger:
                    self.event_logger.emit(
                        AgentTraceEvent(
                            event_type="agent.compaction",
                            app_id=self.agent.config.app_id,
                            session_id=ctx.session_id,
                            trace_id=None,
                            payload={
                                "reason": "auto",
                                "summary_turn_id": summary_turn_id,
                                "compaction_token_threshold": self.agent.config.compaction_token_threshold,
                                "session_total_tokens_before_compaction": session_total_tokens,
                                "turn_limit": self.agent.config.compaction_turn_limit,
                            },
                        )
                    )

        # Create context with user message
        context = Context(system_prompt=self.agent.config.system_prompt)
        # Prepend recent conversation turns for continuity (e.g., "yes" responses).
        if ctx.store and self.agent.config.conversation_history_turns > 0:
            turns = ctx.store.get_turns(session_id=ctx.session_id, limit=None)
            if turns:
                summary_index = None
                for idx in range(len(turns) - 1, -1, -1):
                    meta = turns[idx].get("metadata") or {}
                    if meta.get("type") == "summary_checkpoint":
                        summary_index = idx
                        break

                if summary_index is not None:
                    tail_turns = turns[summary_index + 1 :]
                    tail_turns = tail_turns[
                        -self.agent.config.conversation_history_turns :
                    ]
                    turns_to_include = [turns[summary_index]] + tail_turns
                else:
                    turns_to_include = turns[
                        -self.agent.config.conversation_history_turns :
                    ]

                for turn in turns_to_include:
                    meta = turn.get("metadata") or {}
                    user_text = turn.get("user_message")
                    if user_text and meta.get("type") != "summary_checkpoint":
                        context.add_message(
                            MessageRole.USER,
                            user_text,
                            source="history",
                            turn_id=turn.get("turn_id"),
                        )
                    agent_text = turn.get("agent_response")
                    if agent_text:
                        context.add_message(
                            MessageRole.ASSISTANT,
                            agent_text,
                            source="history",
                            turn_id=turn.get("turn_id"),
                        )
        context.add_user_message(message)

        # Run agent
        response = self.agent.run(context)

        # Render response
        if response.text:
            ctx.renderer.markdown(response.text)

        # Save turn if store available
        if ctx.store:
            token_usage = None
            if response.metadata:
                token_usage = response.metadata.get("token_usage")
            trace_id = None
            if response.metadata:
                trace_id = response.metadata.get("trace_id")
            total_tokens = 0
            if token_usage:
                input_tokens = token_usage.get("input")
                output_tokens = token_usage.get("output")
                if input_tokens is not None and output_tokens is not None:
                    total_tokens = int(input_tokens) + int(output_tokens)

            session_total_tokens = self._get_session_total_tokens(ctx) + total_tokens
            metadata = dict(response.metadata or {})
            metadata["token_usage"] = token_usage or {}
            ctx.store.save_turn(
                trace_id=trace_id or str(uuid.uuid4()),
                session_id=ctx.session_id,
                user_message=message,
                agent_response=response.text,
                signals=response.signals,
                session_total_tokens=session_total_tokens,
                metadata=metadata,
            )

    def _register_default_commands(self) -> None:
        """Register default commands available to all apps."""
        self.commands.register(
            Command(
                name="help",
                help="Show available commands",
                handler=self._help_handler,
                aliases=("h", "?"),
            )
        )

        self.commands.register(
            Command(
                name="exit",
                help="Exit the application",
                handler=self._exit_handler,
                aliases=("quit", "q"),
            )
        )

        self.commands.register(
            Command(
                name="clear",
                help="Clear the screen",
                handler=self._clear_handler,
                aliases=("cls",),
            )
        )

        self.commands.register(
            Command(
                name="session",
                help="Show current session info",
                handler=self._session_handler,
            )
        )

        self.commands.register(
            Command(
                name="preferences",
                help="View or set user preferences",
                handler=self._preferences_handler,
                aliases=("prefs",),
            )
        )

        self.commands.register(
            Command(
                name="app_data",
                help="Manage app-specific data",
                handler=self._app_data_handler,
                aliases=("data",),
            )
        )

        self.commands.register(
            Command(
                name="conversation",
                help="View conversation history",
                handler=self._conversation_handler,
                aliases=("history", "conv"),
            )
        )

        self.commands.register(
            Command(
                name="compact",
                help="Summarize conversation and save a checkpoint",
                handler=self._compact_handler,
                aliases=("compaction",),
            )
        )

    def _help_handler(self, ctx: CLIContext, _args: list[str]) -> None:
        """Show help for commands."""
        commands = self.commands.list_commands()

        if not commands:
            ctx.renderer.info("No commands available.")
            return

        ctx.renderer.info("Available commands:")
        for cmd in commands:
            aliases = f" (aliases: {', '.join(cmd.aliases)})" if cmd.aliases else ""
            ctx.renderer.print(f"  /{cmd.name}{aliases} - {cmd.help}")

    def _exit_handler(self, ctx: CLIContext, args: list[str]) -> None:
        """Exit the application."""
        raise SystemExit(0)

    def _clear_handler(self, ctx: CLIContext, _args: list[str]) -> None:
        """Clear the screen."""
        ctx.renderer.clear()

    def _session_handler(self, ctx: CLIContext, _args: list[str]) -> None:
        """Show session information."""
        ctx.renderer.info(f"App: {ctx.app_name}")
        ctx.renderer.info(f"Session ID: {ctx.session_id}")
        ctx.renderer.info(f"Model: {self.agent.config.model}")
        ctx.renderer.info(f"Max steps: {self.agent.config.max_steps}")
        if ctx.store:
            ctx.renderer.info(f"Session tokens: {self._get_session_total_tokens(ctx)}")

    def _get_session_total_tokens(self, ctx: CLIContext) -> int:
        """Get total tokens used for the current session from the latest turn."""
        if not ctx.store:
            return 0
        turns = ctx.store.get_turns(session_id=ctx.session_id, limit=1)
        if not turns:
            return 0
        value = turns[-1].get("session_total_tokens", 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _preferences_handler(self, ctx: CLIContext, args: list[str]) -> None:
        """Handle /preferences command.

        Usage:
            /preferences              - Show current preferences
            /preferences set <json>   - Set preferences
            /preferences clear        - Clear preferences
        """
        if not args:
            # Show current preferences
            prefs = self.store.get_preferences(
                app_id=self.agent.config.app_id,
                session_id=self.session_id,
            )
            if prefs:
                ctx.renderer.info("Current preferences:")
                ctx.renderer.print(json.dumps(prefs, indent=2))
            else:
                ctx.renderer.warn("No preferences set.")
            return

        subcommand = args[0].lower()

        if subcommand == "set":
            if len(args) < 2:
                ctx.renderer.error("Usage: /preferences set <json>")
                return
            try:
                prefs_json = " ".join(args[1:])
                prefs = json.loads(prefs_json)
                if not isinstance(prefs, dict):
                    ctx.renderer.error("Preferences must be a JSON object")
                    return
                self.store.set_preferences(
                    app_id=self.agent.config.app_id,
                    session_id=self.session_id,
                    preferences=prefs,
                )
                ctx.renderer.info("Preferences saved successfully.")
            except json.JSONDecodeError as e:
                ctx.renderer.error(f"Invalid JSON: {e}")

        elif subcommand == "clear":
            self.store.set_preferences(
                app_id=self.agent.config.app_id,
                session_id=self.session_id,
                preferences={},
            )
            ctx.renderer.info("Preferences cleared.")

        else:
            ctx.renderer.error(f"Unknown subcommand: {subcommand}")
            ctx.renderer.info("Usage: /preferences [set <json> | clear]")

    def _app_data_handler(self, ctx: CLIContext, args: list[str]) -> None:
        """Handle /app_data command.

        Usage:
            /app_data list              - List all app data
            /app_data get <key>         - Get specific data by key
            /app_data set <key> <json>  - Set data by key
            /app_data delete <key>      - Delete data by key
        """
        if not args:
            # Default to list
            args = ["list"]

        subcommand = args[0].lower()

        if subcommand == "list":
            data = self.store.list_app_data(
                app_id=self.agent.config.app_id,
                session_id=self.session_id,
            )
            if data:
                ctx.renderer.info(f"App data ({len(data)} entries):")
                for entry in data:
                    ctx.renderer.print(
                        f"  {entry['key']}: {json.dumps(entry['value'])}"
                    )
            else:
                ctx.renderer.warn("No app data stored.")

        elif subcommand == "get":
            if len(args) < 2:
                ctx.renderer.error("Usage: /app_data get <key>")
                return
            key = args[1]
            value = self.store.get_app_data(
                app_id=self.agent.config.app_id,
                session_id=self.session_id,
                key=key,
            )
            if value is not None:
                ctx.renderer.info(f"Value for '{key}':")
                ctx.renderer.print(json.dumps(value, indent=2))
            else:
                ctx.renderer.warn(f"No data found for key: {key}")

        elif subcommand == "set":
            if len(args) < 3:
                ctx.renderer.error("Usage: /app_data set <key> <json>")
                return
            key = args[1]
            value_json = " ".join(args[2:])
            try:
                value = json.loads(value_json)
                self.store.set_app_data(
                    app_id=self.agent.config.app_id,
                    session_id=self.session_id,
                    key=key,
                    value=value,
                )
                ctx.renderer.info(f"Data stored for key: {key}")
            except json.JSONDecodeError as e:
                ctx.renderer.error(f"Invalid JSON: {e}")

        elif subcommand == "delete":
            if len(args) < 2:
                ctx.renderer.error("Usage: /app_data delete <key>")
                return
            key = args[1]
            deleted = self.store.delete_app_data(
                app_id=self.agent.config.app_id,
                session_id=self.session_id,
                key=key,
            )
            if deleted:
                ctx.renderer.info(f"Data deleted for key: {key}")
            else:
                ctx.renderer.warn(f"No data found for key: {key}")

        else:
            ctx.renderer.error(f"Unknown subcommand: {subcommand}")
            ctx.renderer.info(
                "Usage: /app_data [list | get <key> | set <key> <json> | delete <key>]"
            )

    def _conversation_handler(self, ctx: CLIContext, args: list[str]) -> None:
        """Handle /conversation command.

        Usage:
            /conversation           - Show full conversation history
            /conversation <limit>   - Show last N turns
        """
        limit = None
        if args:
            try:
                limit = int(args[0])
            except ValueError:
                ctx.renderer.error("Limit must be a number")
                return

        turns = self.store.get_turns(
            session_id=self.session_id,
            limit=limit,
        )

        if not turns:
            ctx.renderer.warn("No conversation history.")
            return

        ctx.renderer.info(f"Conversation history ({len(turns)} turns):")
        for i, turn in enumerate(turns, 1):
            ctx.renderer.print(f"\n--- Turn {i} ---")
            ctx.renderer.print(f"User: {turn['user_message']}")
            ctx.renderer.print(f"Agent: {turn['agent_response']}")

    def _compact_handler(self, ctx: CLIContext, _args: list[str]) -> None:
        """Handle /compact command."""
        if not ctx.store:
            ctx.renderer.warn("No conversation store available.")
            return

        summary_text, turn_id = compact_conversation(
            store=ctx.store,
            llm=self.agent.llm,
            app_id=self.agent.config.app_id,
            session_id=ctx.session_id,
            model=self.agent.config.model,
            max_tokens=self.agent.config.max_tokens,
            temperature=self.agent.config.compaction_temperature,
            turn_limit=self.agent.config.compaction_turn_limit,
            reason="manual",
            session_total_tokens_reset=0,
        )

        if not summary_text:
            ctx.renderer.warn("No conversation history to compact.")
            return

        ctx.renderer.info(f"Conversation compacted (turn_id={turn_id}).")
        ctx.renderer.markdown(summary_text)
