"""Background requests in the REPL: /bg parsing, id prefixes, and watching."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SHORT_ID_LENGTH = 8

BG_USAGE = "Usage: /bg <message> | /bg --list | /bg --switch <id> | /bg --kill <id>"


def short_id(request_id: str) -> str:
    return str(request_id or "")[:SHORT_ID_LENGTH]


@dataclass(frozen=True)
class BgCommand:
    """One parsed /bg input: ``start`` a message, or ``list``/``switch``/``kill``."""

    action: str
    message: str = ""
    request_id: str = ""


def parse_bg_args(args: list[str]) -> BgCommand | str:
    """Parse /bg's arguments, or return a usage error.

    Only a first word starting with ``--`` is a flag, so ``/bg list my files``
    is a message.
    """
    if not args:
        return BG_USAGE
    flag = args[0]
    if not flag.startswith("--"):
        return BgCommand(action="start", message=" ".join(args))
    if flag == "--list":
        return BgCommand(action="list") if len(args) == 1 else BG_USAGE
    if flag in ("--switch", "--kill"):
        if len(args) != 2:
            return BG_USAGE
        return BgCommand(action=flag[2:], request_id=args[1])
    return BG_USAGE


def resolve_prefix(prefix: str, records: list[dict[str, Any]]) -> dict[str, Any] | str:
    """The one record whose request id starts with ``prefix``, or an error."""
    matches = [r for r in records if str(r.get("request_id") or "").startswith(prefix)]
    if not matches:
        return f"No background request matches '{prefix}'."
    if len(matches) > 1:
        ids = ", ".join(short_id(r["request_id"]) for r in matches)
        return f"'{prefix}' matches {len(matches)} requests ({ids}); type more of the id."
    return matches[0]


def _clip(text: str, width: int = 60) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def list_rows(records: list[dict[str, Any]]) -> list[list[str]]:
    """Table rows for /bg --list, with each child indented under its parent.

    Records come newest first. A child whose parent is not listed (it runs in
    the foreground) is shown at the top level.
    """
    listed = {r["request_id"] for r in records}
    children: dict[str, list[dict[str, Any]]] = {}
    top: list[dict[str, Any]] = []
    for record in records:
        parent = record.get("parent_request_id")
        if parent and parent in listed:
            children.setdefault(parent, []).append(record)
        else:
            top.append(record)

    def _row(record: dict[str, Any], indent: str) -> list[str]:
        return [
            f"{indent}{short_id(record['request_id'])}",
            _clip(record.get("message") or ""),
            str(record.get("status") or ""),
            str(record.get("started_by") or ""),
        ]

    rows: list[list[str]] = []
    for record in top:
        rows.append(_row(record, ""))
        kids = sorted(
            children.get(record["request_id"], []),
            key=lambda r: (r.get("started_at") or 0.0, r["request_id"]),
        )
        for kid in kids:
            rows.append(_row(kid, "  └ "))
    return rows


# One notice: (kind, text) pairs the REPL prints, kind being info, warn,
# error or markdown.
Notice = list[tuple[str, str]]


class BackgroundWatchers:
    """One task per running bg request the user started.

    Each task reads its request's stream and tells the REPL how it ends, or
    that it needs input. It prints nothing else: a bg request's steps are
    shown when the user switches to it.
    """

    def __init__(
        self,
        client: Any,
        notify: Callable[[Notice], None],
        *,
        input_delay: float = 0.5,
    ) -> None:
        self._client = client
        self._notify = notify
        self._input_delay = input_delay
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def watch(self, agent_id: str, request_id: str, *, after: int = 0) -> None:
        """Start watching a request, from the event after seq ``after``."""
        current = self._tasks.get(request_id)
        if current is not None and not current.done():
            return
        self._tasks[request_id] = asyncio.get_running_loop().create_task(
            self._watch(agent_id, request_id, after),
            name=f"mash-bg-{short_id(request_id)}",
        )

    def silence(self, request_id: str) -> None:
        """Stop watching a request; nothing more is printed about it."""
        task = self._tasks.pop(request_id, None)
        if task is not None:
            task.cancel()

    def watching(self) -> list[str]:
        return [request_id for request_id, task in self._tasks.items() if not task.done()]

    def refresh(self, records: list[dict[str, Any]]) -> None:
        """Watch every running bg request in ``records`` the user started."""
        for record in records:
            if (
                record.get("mode") == "bg"
                and record.get("status") == "running"
                and record.get("started_by") == "user"
                and record.get("agent_id")
            ):
                self.watch(str(record["agent_id"]), str(record["request_id"]))

    async def close(self) -> None:
        """Stop every watcher."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def join(self) -> None:
        """Wait until every watcher has ended."""
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def _watch(self, agent_id: str, request_id: str, after: int) -> None:
        sid = short_id(request_id)
        loop = asyncio.get_running_loop()
        timers: dict[str, asyncio.TimerHandle] = {}

        def ask(interaction_id: str) -> None:
            timers.pop(interaction_id, None)
            self._notify([("warn", f"[{sid}] needs your input: /bg --switch {sid}")])

        try:
            async for event in self._client.request_events(
                agent_id, request_id, after=after
            ):
                name = str(event.get("event") or "")
                data = event.get("data")
                data = data if isinstance(data, dict) else {}
                if name == "request.interaction.create":
                    # A stream read from its start replays an interaction
                    # answered long ago with its ack right behind it. Wait
                    # briefly for that ack before asking.
                    interaction_id = str(data.get("interaction_id") or "")
                    timers[interaction_id] = loop.call_later(
                        self._input_delay, ask, interaction_id
                    )
                elif name == "request.interaction.ack":
                    timer = timers.pop(str(data.get("interaction_id") or ""), None)
                    if timer is not None:
                        timer.cancel()
                elif name == "request.completed":
                    response = data.get("response")
                    text = (
                        str(response.get("text") or "")
                        if isinstance(response, dict)
                        else str(data.get("text") or "")
                    )
                    notice: Notice = [("info", f"[{sid}] done")]
                    if text.strip():
                        notice.append(("markdown", text))
                    self._notify(notice)
                    return
                elif name == "request.error":
                    error = data.get("error") or "request failed"
                    self._notify([("error", f"[{sid}] failed: {error}")])
                    return
                elif name == "request.cancelled":
                    self._notify([("warn", f"[{sid}] cancelled")])
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._notify([("error", f"[{sid}] stopped watching: {exc}")])
        finally:
            for timer in timers.values():
                timer.cancel()
