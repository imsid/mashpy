"""Slash command framework."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Callable, Dict, List, Tuple

from .context import CLIContext

CommandHandler = Callable[[CLIContext, List[str]], None]


@dataclass(frozen=True)
class Command:
    """Declarative command definition."""

    name: str
    help: str
    handler: CommandHandler
    aliases: Tuple[str, ...] = ()


class CommandBus:
    """Registers and executes slash commands."""

    def __init__(self) -> None:
        self._commands: Dict[str, Command] = {}
        self._lookup: Dict[str, Command] = {}

    def register(self, command: Command) -> None:
        """Register a command and all aliases."""

        primary = self._normalize(command.name)
        if not primary:
            raise ValueError("Command names cannot be empty.")
        self._ensure_slot(primary, command)
        self._commands[primary] = command
        self._lookup[primary] = command
        for alias in command.aliases:
            alias_key = self._normalize(alias)
            if not alias_key:
                continue
            self._ensure_slot(alias_key, command)
            self._lookup[alias_key] = command

    def list_commands(self) -> List[Command]:
        """Return all commands sorted by canonical name."""

        return sorted(self._commands.values(), key=lambda cmd: cmd.name)

    def try_execute(self, ctx: CLIContext, line: str) -> bool:
        """Execute ``line`` if it encodes a slash command."""

        raw = line.strip()
        if not raw or not raw.startswith("/"):
            return False
        payload = raw[1:].strip()
        if not payload:
            ctx.renderer.warn("Unknown command: /. Try /help.")
            return True
        parts = payload.split()
        name_token = parts[0]
        args = parts[1:]
        normalized = self._normalize(name_token)
        command = self._lookup.get(normalized)
        if command is None:
            ctx.renderer.warn(f"Unknown command: /{name_token}. Try /help.")
            return True
        start = perf_counter()
        ctx.logger.info("command.start", command=command.name, args=args)
        ok = True
        try:
            command.handler(ctx, args)
        except Exception as exc:  # pylint: disable=broad-except
            ok = False
            ctx.renderer.error(f"{command.name} failed: {exc}")
            ctx.logger.error(
                "command.error",
                command=command.name,
                args=args,
                error=str(exc),
            )
        finally:
            duration_ms = int((perf_counter() - start) * 1000)
            ctx.logger.info(
                "command.end",
                command=command.name,
                ok=ok,
                duration_ms=duration_ms,
            )
        return True

    def _ensure_slot(self, key: str, command: Command) -> None:
        """Ensure that ``key`` is unused or already mapped to the command."""

        existing = self._lookup.get(key)
        if existing is not None and existing is not command:
            raise ValueError(f"Command or alias '{key}' already registered.")

    @staticmethod
    def _normalize(name: str) -> str:
        """Normalize an incoming command name."""

        return name.strip().lstrip("/").lower()
