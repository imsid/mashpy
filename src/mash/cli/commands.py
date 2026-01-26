"""Command system for CLI applications."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from mash.logging.events import CommandEvent

if TYPE_CHECKING:
    from ..logging import EventLogger
    from .app import CLIContext

CommandHandler = Callable[["CLIContext", List[str]], None]


@dataclass(frozen=True)
class Command:
    """Command definition."""

    name: str
    help: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()


class CommandRegistry:
    """Registry for managing commands."""

    def __init__(
        self,
        app_id: str,
        event_logger: Optional[EventLogger] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Initialize command registry.

        Args:
            event_logger: Optional event logger for logging command execution.
            session_id: Optional session ID for event logging.
            app_id: Optional app ID for event logging.
        """
        self._commands: Dict[str, Command] = {}
        self._lookup: Dict[str, Command] = {}
        self._event_logger = event_logger
        self._session_id = session_id
        self._app_id = app_id

    def register(self, command: Command) -> None:
        """Register a command.

        Args:
            command: Command to register.

        Raises:
            ValueError: If command name is empty or already registered.
        """
        name = self._normalize(command.name)
        if not name:
            raise ValueError("Command name cannot be empty")

        if name in self._commands:
            raise ValueError(f"Command '{name}' is already registered")

        self._commands[name] = command
        self._lookup[name] = command

        # Register aliases
        for alias in command.aliases:
            alias_key = self._normalize(alias)
            if alias_key:
                self._lookup[alias_key] = command

    def unregister(self, name: str) -> None:
        """Unregister a command.

        Args:
            name: Command name to unregister.
        """
        name = self._normalize(name)
        self._commands.pop(name, None)
        # Remove from lookup
        to_remove = [k for k, v in self._lookup.items() if v.name == name]
        for k in to_remove:
            self._lookup.pop(k, None)

    def get(self, name: str) -> Command | None:
        """Get a command by name or alias.

        Args:
            name: Command name or alias.

        Returns:
            Command if found, None otherwise.
        """
        return self._lookup.get(self._normalize(name))

    def list_commands(self) -> List[Command]:
        """List all registered commands.

        Returns:
            List of commands sorted by name.
        """
        return sorted(self._commands.values(), key=lambda c: c.name)

    def execute(self, ctx: CLIContext, line: str) -> bool:
        """Execute a command if the line is a command.

        Args:
            ctx: CLI context.
            line: Input line.

        Returns:
            True if line was a command, False otherwise.
        """
        line = line.strip()
        if not line or not line.startswith("/"):
            return False

        # Parse command
        payload = line[1:].strip()
        if not payload:
            ctx.renderer.warn("Unknown command. Try /help.")
            return True

        parts = payload.split()
        cmd_name = parts[0]
        args = parts[1:]

        # Find command
        command = self.get(cmd_name)
        if not command:
            ctx.renderer.warn(f"Unknown command: /{cmd_name}. Try /help.")
            return True

        # Execute command with logging
        start_time = time.time()
        command_name = f"/{command.name}"
        args_str = " ".join(args)

        # Log command start
        if self._event_logger:

            self._event_logger.emit(
                CommandEvent(
                    event_type="command.start",
                    app_id=self._app_id,
                    session_id=self._session_id,
                    command_name=command_name,
                    args=args_str,
                )
            )

        try:
            command.handler(ctx, args)

            # Log command completion
            if self._event_logger:

                self._event_logger.emit(
                    CommandEvent(
                        event_type="command.complete",
                        app_id=self._app_id,
                        session_id=self._session_id,
                        command_name=command_name,
                        duration_ms=int((time.time() - start_time) * 1000),
                    )
                )
        except Exception as e:
            ctx.renderer.error(f"Command failed: {str(e)}")

            # Log command error
            if self._event_logger:

                self._event_logger.emit(
                    CommandEvent(
                        event_type="command.error",
                        app_id=self._app_id,
                        session_id=self._session_id,
                        command_name=command_name,
                        error=str(e),
                        duration_ms=int((time.time() - start_time) * 1000),
                    )
                )

        return True

    @staticmethod
    def _normalize(name: str) -> str:
        """Normalize command name."""
        return name.lower().strip()
