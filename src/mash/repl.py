"""Interactive REPL loop."""

from __future__ import annotations

from pathlib import Path
from typing import List

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from .commands import CommandBus
from .context import CLIContext
from .router import CommandRouter


class Repl:
    """Blocking slash-command REPL."""

    @staticmethod
    def run(
        ctx: CLIContext,
        command_bus: CommandBus,
        *,
        router: CommandRouter | None = None,
    ) -> None:
        """Run the REPL until the user exits."""

        ctx.renderer.info(f"{ctx.app_name} interactive session. Type /help.")
        command_words = _command_word_list(command_bus)
        completer = WordCompleter(command_words, ignore_case=True)
        history = FileHistory(str(_history_path(ctx.app_name)))
        key_bindings = _build_key_bindings(ctx)
        prompt_style = Style.from_dict({"prompt": "bold cyan"})
        session = PromptSession(
            history=history,
            completer=completer,
            auto_suggest=AutoSuggestFromHistory(),
        )
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
            stripped = line.strip()
            if not stripped:
                continue
            try:
                if stripped.startswith("/"):
                    handled = (
                        command_bus.try_execute(ctx, stripped)
                        if router is None
                        else router.route(ctx, stripped)
                    )
                else:
                    if router is None:
                        handled = command_bus.try_execute(ctx, stripped)
                    else:
                        with ctx.renderer.status("Thinking..."):
                            handled = router.route(ctx, stripped)
            except SystemExit:
                ctx.renderer.warn("Bye.")
                raise
            if not handled:
                ctx.renderer.warn("Only slash commands are supported. Try /help.")


def _command_word_list(command_bus: CommandBus) -> List[str]:
    commands: List[str] = []
    for command in command_bus.list_commands():
        commands.append(f"/{command.name}")
        for alias in command.aliases:
            commands.append(f"/{alias}")
    return sorted(set(commands))


def _history_path(app_name: str) -> Path:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in app_name)
    slug = slug.strip("_") or "mash"
    return Path(f".{slug}_history")


def _build_key_bindings(ctx: CLIContext) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("c-l")
    def _(event) -> None:
        del event
        ctx.renderer.clear()

    return bindings
