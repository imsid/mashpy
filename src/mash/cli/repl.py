"""Interactive REPL for CLI applications."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, List, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

if TYPE_CHECKING:
    from .types import CLIContext
    from .commands import CommandRegistry

MessageHandler = Callable[["CLIContext", str], None]


class REPL:
    """Interactive read-eval-print loop."""

    def __init__(
        self,
        app_id: str,
        command_registry: CommandRegistry,
        message_handler: Optional[MessageHandler] = None,
    ) -> None:
        """Initialize REPL.

        Args:
            app_id: Application ID for banner text and history file.
            command_registry: Command registry for command completion.
            message_handler: Handler for non-command messages.
        """
        self.app_id = app_id
        self.command_registry = command_registry
        self.message_handler = message_handler

    def run(self, ctx: CLIContext) -> None:
        """Run the REPL until user exits.

        Args:
            ctx: CLI context.
        """
        ctx.renderer.info(
            f"Connected to {self.app_id}. Type /help for commands."
        )

        # Setup prompt
        command_words = self._get_command_words()
        completer = WordCompleter(command_words, ignore_case=True)
        history = FileHistory(str(self._get_history_path()))
        key_bindings = self._build_key_bindings(ctx)
        prompt_style = Style.from_dict({"prompt": "bold cyan"})

        session: PromptSession[str] = PromptSession(
            history=history,
            completer=completer,
            auto_suggest=AutoSuggestFromHistory(),
        )

        # Main loop
        while True:
            try:
                with patch_stdout():
                    line = session.prompt(
                        [("class:prompt", "> ")],
                        style=prompt_style,
                        key_bindings=key_bindings,
                        complete_while_typing=True,
                    )
            except (KeyboardInterrupt, EOFError):
                ctx.renderer.warn("Bye.")
                return

            line = line.strip()
            if not line:
                continue

            try:
                # Handle commands
                if line.startswith("/"):
                    self.command_registry.execute(ctx, line)
                # Handle messages
                elif self.message_handler:
                    self.message_handler(ctx, line)
                else:
                    ctx.renderer.warn("Only slash commands are supported. Try /help.")
            except SystemExit:
                ctx.renderer.warn("Bye.")
                raise
            except Exception as e:
                ctx.renderer.error(f"Error: {str(e)}")

    def _get_command_words(self) -> List[str]:
        """Get list of command words for completion."""
        words: List[str] = []
        for command in self.command_registry.list_commands():
            words.append(f"/{command.name}")
            for alias in command.aliases:
                words.append(f"/{alias}")
        return sorted(set(words))

    def _get_history_path(self) -> Path:
        """Get path to history file."""
        slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in self.app_id)
        slug = slug.strip("_") or "mash"
        return Path(f".{slug}_history")

    def _build_key_bindings(self, ctx: CLIContext) -> KeyBindings:
        """Build key bindings for the REPL."""
        bindings = KeyBindings()

        @bindings.add("c-l")
        def _clear_screen(_event: Any) -> None:
            """Clear screen on Ctrl+L."""
            ctx.renderer.clear()

        return bindings
