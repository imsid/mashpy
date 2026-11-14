"""Interactive REPL for CLI applications."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List, Optional

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from .input import PROMPT, InputLine, MenuEntry, history_path

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

        prompt_style = Style.from_dict({"prompt": "bold cyan"})
        line_input = InputLine(
            history=history_path(self.app_id),
            commands=self._command_menu,
            key_bindings=self._build_key_bindings(ctx),
        )

        # Main loop
        while True:
            try:
                # raw: notices that background threads print above the
                # prompt keep their colors instead of showing escaped codes.
                with patch_stdout(raw=True):
                    line = line_input.prompt(
                        [("class:prompt", PROMPT)], style=prompt_style
                    )
            except (KeyboardInterrupt, EOFError):
                ctx.renderer.warn("Bye.")
                return

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

    def _command_menu(self) -> List[MenuEntry]:
        """Each command's name and help, for the ``/`` menu."""
        return [
            (command.name, command.help)
            for command in self.command_registry.list_commands()
        ]

    def _build_key_bindings(self, ctx: CLIContext) -> KeyBindings:
        """Build key bindings for the REPL."""
        bindings = KeyBindings()

        @bindings.add("c-l")
        def _clear_screen(_event: Any) -> None:
            """Clear screen on Ctrl+L."""
            ctx.renderer.clear()

        return bindings
