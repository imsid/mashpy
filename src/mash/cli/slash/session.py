"""Session slash commands."""

from __future__ import annotations

from ..commands import Command
from ..render import fmt_tokens
from .groups import GROUP_SESSION


def register(shell) -> None:
    def add(
        name: str, help: str, handler, group: str, aliases: tuple[str, ...] = ()
    ) -> None:
        shell.command_registry.register(
            Command(name=name, help=help, handler=handler, aliases=aliases, group=group)
        )

    async def new_command(ctx, args: list[str]) -> None:
        del ctx, args
        await shell.switch_session(shell.new_session_id(), resumed=False)

    async def resume_command(ctx, args: list[str]) -> None:
        del ctx, args
        await shell.pick_session()

    async def compact_command(ctx, args: list[str]) -> None:
        del ctx, args
        await shell.compact()

    def usage_command(ctx, _args: list[str]) -> None:
        session = ctx.client.get_session(ctx.agent_id, ctx.session_id)
        show_usage(ctx.renderer, ctx.session_id, session.get("usage") or {})

    def copy_command(ctx, args: list[str]) -> None:
        if args not in ([], ["code"]):
            ctx.renderer.error("Usage: /copy | /copy code")
            return
        shell.copy_last(code=bool(args))

    add("new", "Start a new session", new_command, GROUP_SESSION)
    add(
        "resume", "Go on in one of your earlier sessions", resume_command, GROUP_SESSION
    )
    add(
        "compact",
        "Summarize the session so far to free up context",
        compact_command,
        GROUP_SESSION,
    )
    add(
        "usage",
        "Tokens this session used, by agent, subagents included",
        usage_command,
        GROUP_SESSION,
    )
    add(
        "copy",
        "Copy the last answer, or its last code block (/copy code)",
        copy_command,
        GROUP_SESSION,
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def show_usage(renderer, session_id: str, usage: dict) -> None:
    """The session's tokens: one row per agent, its subagents under it."""
    agents = [a for a in usage.get("agents") or [] if isinstance(a, dict)]
    requests = int(usage.get("request_count") or 0)
    runs = int(usage.get("subagent_count") or 0)
    summary = [_plural(requests, "request")]
    if runs:
        summary.append(_plural(runs, "subagent run"))
    summary.append(f"{fmt_tokens(int(usage.get('total_tokens') or 0))} tokens")
    renderer.section(f"Session {session_id}")
    renderer.muted(" · ".join(summary))
    if not agents:
        return

    def counts(row: dict) -> list[str]:
        return [
            fmt_tokens(int(row.get(key) or 0))
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            )
        ]

    rows = []
    for agent in agents:
        subagent = not agent.get("request_count")
        name = str(agent.get("agent_id") or "")
        runs_here = int(agent.get("request_count") or 0) + int(
            agent.get("subagent_count") or 0
        )
        rows.append(
            [
                f"↳ {name}" if subagent else name,
                ", ".join(agent.get("models") or []) or "—",
                str(runs_here),
                *counts(agent),
            ]
        )
    footer = None
    if len(rows) > 1:
        footer = ["Total", "", str(requests + runs), *counts(usage)]
    renderer.table(
        ["Agent", "Model", "Runs", "In", "Out", "Cache read", "Cache write"],
        rows,
        numeric=5,
        footer=footer,
    )
