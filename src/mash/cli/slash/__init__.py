"""Built-in slash commands, grouped as they appear in /help."""

from . import deployment, repl, requests, session


def register_default_commands(shell) -> None:
    for group in (session, requests, deployment, repl):
        group.register(shell)
