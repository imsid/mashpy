"""Base application class for building CLI agents."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from ..core.agent import Agent
from ..core.context import Context
from ..memory.store import ConversationStore
from .commands import Command, CommandRegistry
from .render import RichRenderer
from .repl import REPL


@dataclass
class CLIContext:
    """Context for CLI operations."""

    app_name: str
    session_id: str
    renderer: RichRenderer
    agent: Optional[Agent] = None
    store: Optional[ConversationStore] = None


class MashApp:
    """Base class for building agent-powered CLI applications."""

    def __init__(
        self,
        app_name: str,
        agent: Agent,
        store: ConversationStore,
    ) -> None:
        """Initialize the application.

        Args:
            app_name: Application name.
            agent: Agent instance.
            store: Conversation store.
        """
        self.app_name = app_name
        self.agent = agent
        self.store = store
        self.session_id = str(uuid.uuid4())

        # Initialize components
        self.renderer = RichRenderer()
        self.commands = CommandRegistry()
        self.context = CLIContext(
            app_name=app_name,
            session_id=self.session_id,
            renderer=self.renderer,
            agent=agent,
            store=store,
        )

        # Register default commands
        self._register_default_commands()

        # Allow subclasses to register their commands
        self.register_commands()

    def register_commands(self) -> None:
        """Register application-specific commands.

        Override this in subclasses to add custom commands.
        """
        pass

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
        # Create context with user message
        context = Context(system_prompt=self.agent.config.system_prompt)
        context.add_user_message(message)

        # Run agent
        response = self.agent.run(context)

        # Render response
        if response.text:
            ctx.renderer.markdown(response.text)

        # Save turn if store available
        if ctx.store:
            ctx.store.save_turn(
                session_id=ctx.session_id,
                user_message=message,
                agent_response=response.text,
                signals=response.signals,
                metadata=response.metadata,
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

    def _help_handler(self, ctx: CLIContext, args: list[str]) -> None:
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

    def _clear_handler(self, ctx: CLIContext, args: list[str]) -> None:
        """Clear the screen."""
        ctx.renderer.clear()

    def _session_handler(self, ctx: CLIContext, args: list[str]) -> None:
        """Show session information."""
        ctx.renderer.info(f"App: {ctx.app_name}")
        ctx.renderer.info(f"Session ID: {ctx.session_id}")
        ctx.renderer.info(f"Model: {self.agent.config.model}")
        ctx.renderer.info(f"Max steps: {self.agent.config.max_steps}")
