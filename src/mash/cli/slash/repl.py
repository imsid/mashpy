"""Repl slash commands."""

from __future__ import annotations

from rich.markup import escape

from ..commands import Command, ExitRepl
from .groups import GROUP_DEPLOYMENT, GROUP_REPL, GROUP_REQUESTS, GROUP_SESSION

KEYS = (
    ("Enter", "Send the message"),
    ("Alt+Enter, Ctrl-J, \\ then Enter", "Add a line"),
    ("Up, Down", "Move between lines, then through history"),
    ("Shift+arrows", "Select; typing replaces the selection"),
    ("Ctrl-W, Ctrl-K, Ctrl-U", "Cut a word, to the end of the line, to its start"),
    ("Ctrl-Y", "Paste what was cut"),
    ("Ctrl-_", "Undo"),
    ("Ctrl-X Ctrl-E", "Edit the message in $EDITOR"),
    ("/", "Open the command menu"),
    ("Ctrl-C", "Clear the input, then cancel the request, then (twice) leave"),
    ("Esc", "Cancel the running request"),
    ("Ctrl-O", "Show or hide model steps and subagent tool calls"),
    ("Ctrl-L", "Clear the screen"),
    ("Ctrl-D", "Leave"),
)


def register(shell) -> None:
    def add(
        name: str, help: str, handler, group: str, aliases: tuple[str, ...] = ()
    ) -> None:
        shell.command_registry.register(
            Command(name=name, help=help, handler=handler, aliases=aliases, group=group)
        )

    def help_command(ctx, _args: list[str]) -> None:
        groups: dict[str, list[Command]] = {}
        for command in shell.command_registry.list_commands():
            groups.setdefault(command.group or "More", []).append(command)
        builtin = [GROUP_SESSION, GROUP_REQUESTS, GROUP_DEPLOYMENT]
        # An app's own groups come after the built-in ones, the REPL's last.
        order = (
            builtin + sorted(set(groups) - set(builtin) - {GROUP_REPL}) + [GROUP_REPL]
        )
        width = max((len(c.name) for cs in groups.values() for c in cs), default=0)
        first = True
        for group in order:
            if group not in groups:
                continue
            if not first:
                ctx.renderer.print()
            first = False
            ctx.renderer.info(group)
            for command in groups[group]:
                aliases = (
                    f" (also /{', /'.join(command.aliases)})" if command.aliases else ""
                )
                ctx.renderer.print(
                    f"  [accent]/{command.name:<{width}}[/accent]  "
                    f"{escape(command.help)}[muted]{escape(aliases)}[/muted]"
                )

    def keys_command(ctx, _args: list[str]) -> None:
        ctx.renderer.table(["Key", "Does"], [list(row) for row in KEYS])

    def feedback_command(ctx, args: list[str]) -> None:
        message = " ".join(args).strip()
        if not message:
            ctx.renderer.error("Usage: /feedback <message>")
            return
        try:
            ctx.client.submit_feedback(
                ctx.agent_id,
                message=message,
                host_id=ctx.host_id,
                session_id=ctx.session_id,
                request_id=ctx.last_request_id,
            )
        except Exception as exc:
            ctx.renderer.error(f"Failed to record feedback: {exc}")
            return
        request_note = f", request {ctx.last_request_id}" if ctx.last_request_id else ""
        ctx.renderer.info(
            f"✓ Feedback recorded (session {ctx.session_id}{request_note})"
        )

    def clear_command(ctx, _args: list[str]) -> None:
        ctx.renderer.clear()

    def exit_command(_ctx, _args: list[str]) -> None:
        raise ExitRepl()

    add("help", "Show the commands", help_command, GROUP_REPL, ("h", "?"))
    add("keys", "Show the keys", keys_command, GROUP_REPL)
    add(
        "feedback",
        "Send a note or bug report about this session",
        feedback_command,
        GROUP_REPL,
    )
    add("clear", "Clear the screen", clear_command, GROUP_REPL, ("cls",))
    add("exit", "Leave", exit_command, GROUP_REPL, ("quit", "q"))
