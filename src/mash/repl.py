"""Interactive REPL loop."""

from __future__ import annotations

from .commands import CommandBus
from .context import CLIContext


class Repl:
    """Blocking slash-command REPL."""

    @staticmethod
    def run(ctx: CLIContext, command_bus: CommandBus) -> None:
        """Run the REPL until the user exits."""

        print(f"{ctx.app_name} interactive session. Type /help.")
        while True:
            try:
                line = input("> ")
            except (KeyboardInterrupt, EOFError):
                print("Bye.")
                return
            stripped = line.strip()
            if not stripped:
                continue
            try:
                handled = command_bus.try_execute(ctx, stripped)
            except SystemExit:
                print("Bye.")
                raise
            if not handled:
                print("Only slash commands are supported. Try /help.")
