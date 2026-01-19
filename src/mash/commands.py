"""Slash command framework."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional, Tuple

from .context import CLIContext
from .logging import CommandEvent, EventLogger
from .mashd.tools import ToolResult, ToolSpec, normalize_tool_name

CommandHandler = Callable[[CLIContext, List[str]], None]


@dataclass(frozen=True)
class Command:
    """Declarative command definition."""

    name: str
    help: str
    handler: CommandHandler
    aliases: Tuple[str, ...] = ()
    input_schema: Optional[Dict[str, Any]] = None

    def to_tool_spec(self, *, prefix: str = "cmd") -> ToolSpec:
        safe_prefix = normalize_tool_name(prefix)
        safe_name = normalize_tool_name(self.name)
        tool_name = f"{safe_prefix}_{safe_name}" if safe_prefix else safe_name
        input_schema = self.input_schema or {
            "type": "object",
            "properties": {
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Arguments passed to the command.",
                }
            },
            "required": [],
        }

        def _invoke(args: Dict[str, Any], ctx: Optional[CLIContext]) -> ToolResult:
            if ctx is None:
                return ToolResult(
                    name=tool_name,
                    content="Command tools require CLI context.",
                    is_error=True,
                )
            arg_list = _normalize_command_args(args)
            self.handler(ctx, arg_list)
            return ToolResult(name=tool_name, content="ok")

        return ToolSpec(
            name=tool_name,
            description=self.help,
            input_schema=input_schema,
            source="command",
            metadata={"command": self.name, "aliases": list(self.aliases)},
            invoke=_invoke,
        )


class CommandBus:
    """Registers and executes slash commands."""

    def __init__(self, event_logger: Optional[EventLogger] = None) -> None:
        self._commands: Dict[str, Command] = {}
        self._lookup: Dict[str, Command] = {}
        self._event_logger = event_logger

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
        self._emit_event(ctx, "command.start", {"command": command.name, "args": args})
        ok = True
        try:
            command.handler(ctx, args)
        except Exception as exc:  # pylint: disable=broad-except
            ok = False
            ctx.renderer.error(f"{command.name} failed: {exc}")
            self._emit_event(
                ctx,
                "command.error",
                {"command": command.name, "args": args, "error": str(exc)},
            )
        finally:
            duration_ms = int((perf_counter() - start) * 1000)
            self._emit_event(
                ctx,
                "command.end",
                {
                    "command": command.name,
                    "ok": ok,
                    "duration_ms": duration_ms,
                },
            )
        return True

    def _emit_event(
        self,
        ctx: CLIContext,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        if not self._event_logger:
            return
        event = CommandEvent(
            event_type=event_type,
            app_id=ctx.app_name,
            session_id=ctx.session_id,
            payload=payload,
        )
        self._event_logger.emit(event)

    def _ensure_slot(self, key: str, command: Command) -> None:
        """Ensure that ``key`` is unused or already mapped to the command."""

        existing = self._lookup.get(key)
        if existing is not None and existing is not command:
            raise ValueError(f"Command or alias '{key}' already registered.")

    @staticmethod
    def _normalize(name: str) -> str:
        """Normalize an incoming command name."""

        return name.strip().lstrip("/").lower()


def _normalize_command_args(payload: Dict[str, Any]) -> List[str]:
    if not isinstance(payload, dict):
        return []
    raw_args = payload.get("args")
    if isinstance(raw_args, list):
        return [str(entry) for entry in raw_args]
    if isinstance(raw_args, str):
        return raw_args.split()
    return []
