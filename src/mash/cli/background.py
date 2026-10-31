"""Background requests in the REPL: /bg parsing, Ctrl-Z, and idle watching."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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


class MovedToBackground(Exception):
    """Ctrl-Z was pressed while the shell read a request's stream."""


@contextmanager
def ctrl_z_moves_to_background() -> Iterator[None]:
    """For the length of a stream read, make Ctrl-Z raise ``MovedToBackground``.

    The shell reads the stream outside prompt_toolkit, so its key bindings do
    not apply; the terminal turns Ctrl-Z into SIGTSTP. The handler sets a flag
    and raises, which also interrupts a read that is waiting for the next
    event. The previous handler comes back when the read ends, so at the
    prompt Ctrl-Z keeps its default behavior.
    """
    if (
        not hasattr(signal, "SIGTSTP")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return
    armed = {"value": True}

    def _handler(signum: int, frame: Any) -> None:
        del signum, frame
        if armed["value"]:
            armed["value"] = False
            raise MovedToBackground()

    previous = signal.signal(signal.SIGTSTP, _handler)
    try:
        yield
    finally:
        armed["value"] = False
        signal.signal(signal.SIGTSTP, previous)


# One notice: (kind, text) pairs the shell renders, kind being info, warn,
# error or markdown.
Notice = list[tuple[str, str]]


class RequestWatcher:
    """Follows one bg request's stream on a thread and reports how it ends."""

    def __init__(
        self,
        client: Any,
        agent_id: str,
        request_id: str,
        notify: Callable[[Notice], None],
        *,
        input_delay: float = 0.5,
    ) -> None:
        self.agent_id = agent_id
        self.request_id = request_id
        self._client = client
        self._notify = notify
        self._input_delay = input_delay
        self._silenced = threading.Event()
        self._timers: dict[str, threading.Timer] = {}
        self._thread = threading.Thread(
            target=self._run, name=f"mash-bg-{short_id(request_id)}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def silenced(self) -> bool:
        return self._silenced.is_set()

    def silence(self) -> None:
        """Print nothing more; the thread ends at the stream's next event."""
        self._silenced.set()
        self._cancel_timers()

    def _say(self, notice: Notice) -> None:
        if not self.silenced:
            self._notify(notice)

    def _cancel_timers(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()

    def _ask_for_input(self, interaction_id: str) -> None:
        self._timers.pop(interaction_id, None)
        sid = short_id(self.request_id)
        self._say([("warn", f"[{sid}] needs your input: /bg --switch {sid}")])

    def _run(self) -> None:
        sid = short_id(self.request_id)
        try:
            for event in self._client.stream_request(self.agent_id, self.request_id):
                if self.silenced:
                    return
                name = str(event.get("event") or "")
                data = event.get("data")
                data = data if isinstance(data, dict) else {}
                if name == "request.interaction.create":
                    # The stream replays from the start, so an interaction
                    # answered long ago arrives with its ack right behind it.
                    # Wait briefly for that ack before asking.
                    interaction_id = str(data.get("interaction_id") or "")
                    timer = threading.Timer(
                        self._input_delay, self._ask_for_input, args=(interaction_id,)
                    )
                    timer.daemon = True
                    self._timers[interaction_id] = timer
                    timer.start()
                elif name == "request.interaction.ack":
                    timer = self._timers.pop(str(data.get("interaction_id") or ""), None)
                    if timer is not None:
                        timer.cancel()
                elif name == "request.completed":
                    self._cancel_timers()
                    response = data.get("response")
                    text = (
                        str(response.get("text") or "")
                        if isinstance(response, dict)
                        else str(data.get("text") or "")
                    )
                    notice: Notice = [("info", f"[{sid}] done")]
                    if text.strip():
                        notice.append(("markdown", text))
                    self._say(notice)
                    return
                elif name == "request.error":
                    self._cancel_timers()
                    error = data.get("error") or "request failed"
                    self._say([("error", f"[{sid}] failed: {error}")])
                    return
                elif name == "request.cancelled":
                    self._cancel_timers()
                    self._say([("warn", f"[{sid}] cancelled")])
                    return
        except Exception as exc:
            self._cancel_timers()
            self._say([("error", f"[{sid}] stopped watching: {exc}")])


class BackgroundWatchers:
    """The REPL's watchers, one per running bg request the user started."""

    def __init__(
        self,
        client_factory: Callable[[], Any],
        notify: Callable[[Notice], None],
        *,
        input_delay: float = 0.5,
    ) -> None:
        self._client_factory = client_factory
        self._notify = notify
        self._input_delay = input_delay
        self._lock = threading.Lock()
        self._watchers: dict[str, RequestWatcher] = {}

    def watch(self, agent_id: str, request_id: str) -> None:
        with self._lock:
            current = self._watchers.get(request_id)
            if current is not None and current.alive and not current.silenced:
                return
            watcher = RequestWatcher(
                self._client_factory(),
                agent_id,
                request_id,
                self._notify,
                input_delay=self._input_delay,
            )
            self._watchers[request_id] = watcher
        watcher.start()

    def silence(self, request_id: str) -> None:
        with self._lock:
            watcher = self._watchers.get(request_id)
        if watcher is not None:
            watcher.silence()

    def watching(self) -> list[str]:
        with self._lock:
            return [
                request_id
                for request_id, watcher in self._watchers.items()
                if watcher.alive and not watcher.silenced
            ]

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

    def join(self, timeout: float | None = None) -> None:
        with self._lock:
            watchers = list(self._watchers.values())
        for watcher in watchers:
            watcher.join(timeout)
