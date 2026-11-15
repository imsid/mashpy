"""Session slash commands."""

from __future__ import annotations

from datetime import datetime

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
        total = session.get("total_tokens")
        if total is None:
            total = session.get("session_total_tokens")
        line = f"This session: {fmt_tokens(int(total or 0))} tokens"
        cache_read = int(session.get("cache_read_tokens") or 0)
        cache_write = int(session.get("cache_write_tokens") or 0)
        if cache_read or cache_write:
            line += (
                f" (cache: {fmt_tokens(cache_read)} read, "
                f"{fmt_tokens(cache_write)} written)"
            )
        ctx.renderer.info(line)
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        buckets = ctx.client.get_usage(ctx.agent_id, from_ts=midnight.timestamp())
        requests = sum(int(bucket.get("request_count") or 0) for bucket in buckets)
        tokens_in = sum(int(bucket.get("input_tokens") or 0) for bucket in buckets)
        tokens_out = sum(int(bucket.get("output_tokens") or 0) for bucket in buckets)
        ctx.renderer.info(
            f"Today, {ctx.agent_id}: {requests} request{'' if requests == 1 else 's'} · "
            f"{fmt_tokens(tokens_in)} in · {fmt_tokens(tokens_out)} out"
        )

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
    add("usage", "Tokens used by this session and today", usage_command, GROUP_SESSION)
    add(
        "copy",
        "Copy the last answer, or its last code block (/copy code)",
        copy_command,
        GROUP_SESSION,
    )
