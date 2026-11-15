"""Background requests in the REPL: /bg, Ctrl-Z, and watching."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mash.cli.background import (
    BG_USAGE,
    BackgroundWatchers,
    BgCommand,
    list_rows,
    parse_bg_args,
    resolve_prefix,
)
from mash.cli.client import MashHostClientError
from tests.mash.cli.repl_harness import (
    CTRL_Z,
    ENTER,
    FakeHost,
    Gate,
    ReplDriver,
    chat_end,
    completed,
    interaction,
    run_repl,
    trace,
)

PARENT = "a1b2c3d4-0000-4000-8000-000000000001"
CHILD_1 = "c1c1c1c1-0000-5000-8000-000000000002"
CHILD_2 = "c2c2c2c2-0000-5000-8000-000000000003"
OTHER = "a1b2ffff-0000-4000-8000-000000000004"


def _record(request_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "agent_id": "primary",
        "message": f"message {request_id[:4]}",
        "mode": "bg",
        "status": "running",
        "started_by": "user",
        "parent_request_id": None,
        "started_at": 1.0,
        **fields,
    }


# --- parsing ------------------------------------------------------------------


def test_the_four_forms() -> None:
    assert parse_bg_args(["set", "a", "timer"]) == BgCommand(action="start", message="set a timer")
    assert parse_bg_args(["list"]) == BgCommand(action="list")
    assert parse_bg_args(["switch", "a1b2"]) == BgCommand(action="switch", request_id="a1b2")
    assert parse_bg_args(["kill", "a1b2"]) == BgCommand(action="kill", request_id="a1b2")


def test_a_message_that_starts_with_a_subcommand_word_is_a_message() -> None:
    assert parse_bg_args(["list", "my", "files"]) == BgCommand(
        action="start", message="list my files"
    )
    assert parse_bg_args(["kill", "the", "old", "export"]) == BgCommand(
        action="start", message="kill the old export"
    )
    assert parse_bg_args(["--list"]) == BgCommand(action="start", message="--list")


@pytest.mark.parametrize("args", [[], ["switch"], ["kill"]])
def test_usage_errors(args: list[str]) -> None:
    assert parse_bg_args(args) == BG_USAGE


# --- id prefixes --------------------------------------------------------------


def test_a_unique_prefix_resolves() -> None:
    records = [_record(PARENT), _record(OTHER), _record(CHILD_1)]
    assert resolve_prefix("c1", records)["request_id"] == CHILD_1  # type: ignore[index]
    assert resolve_prefix("a1b2c3", records)["request_id"] == PARENT  # type: ignore[index]


def test_an_ambiguous_prefix_is_an_error_message() -> None:
    message = resolve_prefix("a1b2", [_record(PARENT), _record(OTHER)])
    assert isinstance(message, str)
    assert "matches 2 requests" in message
    assert "a1b2c3d4" in message


def test_an_unknown_prefix_is_an_error_message() -> None:
    assert resolve_prefix("zz", [_record(PARENT)]) == "No background request matches 'zz'."


# --- list rows ----------------------------------------------------------------


def test_children_are_indented_under_their_parent() -> None:
    records = [
        _record(CHILD_2, parent_request_id=PARENT, started_by="primary", started_at=3.0),
        _record(CHILD_1, parent_request_id=PARENT, started_by="primary", started_at=2.0),
        _record(OTHER, status="completed", started_at=1.5),
        _record(PARENT, started_at=1.0),
    ]
    rows = list_rows(records)
    assert [row[0] for row in rows] == ["a1b2ffff", "a1b2c3d4", "  └ c1c1c1c1", "  └ c2c2c2c2"]
    assert rows[0][2:] == ["completed", "user"]
    assert rows[2][3] == "primary"


def test_a_child_of_an_unlisted_parent_is_shown_at_the_top() -> None:
    rows = list_rows([_record(CHILD_1, parent_request_id="fg-parent")])
    assert rows[0][0] == "c1c1c1c1"


# --- watchers -----------------------------------------------------------------


class _StreamClient:
    """Streams scripted events; a ``Gate`` holds the stream open."""

    def __init__(self, *events: Any) -> None:
        self.events = events
        self.calls: list[tuple[str, str, int]] = []

    async def request_events(self, agent_id: str, request_id: str, *, after: int = 0):
        self.calls.append((agent_id, request_id, after))
        for event in self.events:
            if isinstance(event, Gate):
                await event.wait()
            elif isinstance(event, Exception):
                raise event
            else:
                yield event


def _watch(*events: Any, wait: float = 0.0) -> list[list[tuple[str, str]]]:
    notices: list[list[tuple[str, str]]] = []

    async def main() -> None:
        watchers = BackgroundWatchers(_StreamClient(*events), notices.append, input_delay=0.05)
        watchers.watch("primary", PARENT)
        if wait:
            await asyncio.sleep(wait)
            await watchers.close()
        else:
            await watchers.join()

    asyncio.run(main())
    return notices


def test_completed_prints_done_and_the_response() -> None:
    assert _watch(completed("Timer is up.")) == [
        [("info", "[a1b2c3d4] done"), ("markdown", "Timer is up.")]
    ]


def test_failed_and_cancelled() -> None:
    assert _watch({"event": "request.error", "data": {"error": "boom"}}) == [
        [("error", "[a1b2c3d4] failed: boom")]
    ]
    assert _watch({"event": "request.cancelled", "data": {}}) == [
        [("warn", "[a1b2c3d4] cancelled")]
    ]


def test_an_open_interaction_asks_for_input() -> None:
    notices = _watch(interaction("i-1", "approval", "Approve?"), Gate(), wait=0.3)
    assert notices == [[("warn", "[a1b2c3d4] needs your input: /bg switch a1b2c3d4")]]


def test_an_answered_interaction_in_the_replay_is_not_announced() -> None:
    notices = _watch(
        interaction("i-1", "approval", "Approve?"),
        {"event": "request.interaction.ack", "data": {"interaction_id": "i-1"}},
        trace("chat.start"),
        Gate(),
        wait=0.3,
    )
    assert notices == []


def test_a_stream_that_breaks_is_reported() -> None:
    assert _watch(MashHostClientError("connection lost")) == [
        [("error", "[a1b2c3d4] stopped watching: connection lost")]
    ]


def test_a_silenced_watcher_prints_nothing() -> None:
    notices: list[Any] = []
    gate = Gate()

    async def main() -> None:
        watchers = BackgroundWatchers(_StreamClient(gate, completed("x")), notices.append)
        watchers.watch("primary", PARENT)
        await asyncio.sleep(0.01)
        assert watchers.watching() == [PARENT]
        watchers.silence(PARENT)
        gate.open()
        await asyncio.sleep(0.05)
        assert watchers.watching() == []

    asyncio.run(main())
    assert notices == []


def test_a_request_is_watched_once_and_from_the_given_cursor() -> None:
    client = _StreamClient(Gate())

    async def main() -> None:
        watchers = BackgroundWatchers(client, lambda notice: None)
        watchers.watch("primary", PARENT, after=7)
        watchers.watch("primary", PARENT)
        await asyncio.sleep(0.01)
        await watchers.close()

    asyncio.run(main())
    assert client.calls == [("primary", PARENT, 7)]


def test_refresh_watches_only_running_bg_requests_the_user_started() -> None:
    client = _StreamClient(Gate())

    async def main() -> None:
        watchers = BackgroundWatchers(client, lambda notice: None)
        watchers.refresh(
            [
                _record(PARENT),
                _record(OTHER, status="completed"),
                _record(CHILD_1, parent_request_id=PARENT, started_by="primary"),
                _record(CHILD_2, mode="fg"),
            ]
        )
        await asyncio.sleep(0.01)
        assert watchers.watching() == [PARENT]
        await watchers.close()

    asyncio.run(main())


# --- /bg and Ctrl-Z in the REPL -------------------------------------------------


def run(script, host: FakeHost, **kwargs: Any) -> ReplDriver:
    return asyncio.run(run_repl(script, host, **kwargs))


def test_bg_message_submits_in_the_background_and_reports_when_done() -> None:
    host = FakeHost()
    host.request_ids = [PARENT]
    gate = Gate()
    host.script(PARENT, trace("chat.start"), gate, completed("Timer is up."))

    async def script(d: ReplDriver) -> None:
        d.type("/bg set a timer for 1 min" + ENTER)
        await d.until(lambda: "running in the background" in d.output)
        # The prompt is free: nothing runs in the foreground.
        assert not d.repl.running()
        gate.open()
        await d.until(lambda: "[a1b2c3d4] done" in d.output)

    out = run(script, host).output
    assert host.submitted[0]["message"] == "set a timer for 1 min"
    assert host.submitted[0]["mode"] == "bg"
    assert "[a1b2c3d4] running in the background" in out
    assert "Timer is up." in out


def test_bg_message_through_a_host() -> None:
    host = FakeHost()
    host.request_ids = [PARENT]
    host.script(PARENT, completed("ok"))

    async def script(d: ReplDriver) -> None:
        d.type("/bg list my files" + ENTER)
        await d.until(lambda: "[a1b2c3d4] done" in d.output)

    run(script, host, host_id="assistant")
    assert host.submitted[0]["host_id"] == "assistant"
    assert host.submitted[0]["message"] == "list my files"
    assert host.stream_calls == [("companion", PARENT, 0)]


def test_list_shows_children_under_their_parent() -> None:
    host = FakeHost()
    host.records = [
        _record(CHILD_1, parent_request_id=PARENT, started_by="primary", started_at=2.0,
                status="completed"),
        _record(PARENT, started_at=1.0, status="completed"),
    ]

    async def script(d: ReplDriver) -> None:
        await d.say("/bg list")

    lines = run(script, host).output.splitlines()
    parent = next(i for i, line in enumerate(lines) if "a1b2c3d4" in line)
    assert "└ c1c1c1c1" in lines[parent + 1]
    assert "Started by" in "\n".join(lines)


def test_list_with_nothing_in_the_background_says_so() -> None:
    async def script(d: ReplDriver) -> None:
        await d.say("/bg list")

    assert "No background requests in this session." in run(script, FakeHost()).output


def test_switch_brings_a_request_to_the_foreground_and_follows_it() -> None:
    host = FakeHost()
    host.records = [_record(PARENT)]
    gate = Gate()
    host.script(PARENT, chat_end("response", text="Timer is up."), gate, completed("Timer is up."))

    async def script(d: ReplDriver) -> None:
        d.type("/bg switch a1b2" + ENTER)
        await d.until(d.repl.running)
        assert d.repl.foreground.request_id == PARENT
        host.records = []
        gate.open()
        await d.idle()

    out = run(script, host).output
    assert host.modes == [("primary", PARENT, "fg")]
    assert out.count("Timer is up.") == 1
    # Followed in the foreground, so no background notice is printed for it.
    assert "[a1b2c3d4] done" not in out


def test_switch_to_a_child_leaves_its_mode() -> None:
    host = FakeHost()
    host.records = [
        _record(CHILD_1, agent_id="research", parent_request_id=PARENT, started_by="primary")
    ]
    host.script(CHILD_1, completed("r"))

    async def script(d: ReplDriver) -> None:
        d.type("/bg switch c1" + ENTER)
        await d.until(lambda: ("research", CHILD_1, 0) in host.stream_calls)
        await d.idle()

    run(script, host)
    assert host.modes == []


def test_switch_while_a_request_runs_is_refused() -> None:
    host = FakeHost()
    host.records = [_record(PARENT)]
    gate = Gate()
    host.script("r-1", trace("chat.start"), gate, completed("ok"))

    async def script(d: ReplDriver) -> None:
        d.type("hello" + ENTER)
        await d.until(d.repl.running)
        d.type("/bg switch a1b2" + ENTER)
        await d.until(lambda: "Press Ctrl-Z" in d.output)
        host.records = []
        gate.open()
        await d.idle()

    run(script, host)
    assert host.modes == []


def test_switch_and_kill_report_a_bad_prefix() -> None:
    host = FakeHost()
    host.records = [_record(PARENT, status="completed"), _record(OTHER, status="completed")]

    async def script(d: ReplDriver) -> None:
        await d.say("/bg switch a1b2")
        await d.say("/bg kill zz")

    out = run(script, host).output
    assert "'a1b2' matches 2 requests (a1b2c3d4, a1b2ffff)" in out
    assert "No background request matches 'zz'." in out
    assert host.modes == []
    assert host.cancelled == []


def test_kill_calls_the_cancel_route_on_the_requests_agent() -> None:
    host = FakeHost()
    host.records = [
        _record(CHILD_1, agent_id="research", parent_request_id=PARENT, started_by="primary")
    ]

    async def script(d: ReplDriver) -> None:
        await d.say("/bg kill c1c1")

    out = run(script, host).output
    assert host.cancelled == [("research", CHILD_1)]
    assert "[c1c1c1c1] cancelled" in out


def test_a_usage_error_is_printed() -> None:
    async def script(d: ReplDriver) -> None:
        await d.say("/bg")

    assert BG_USAGE in run(script, FakeHost()).output


def test_after_a_foreground_request_running_bg_requests_are_watched() -> None:
    host = FakeHost()
    host.records = [
        _record(PARENT),
        _record(OTHER, status="completed"),
        _record(CHILD_1, parent_request_id=PARENT, started_by="primary"),
    ]
    host.script("r-1", chat_end("response", text="hi"), completed("hi"))
    host.script(PARENT, completed("Background answer."))

    async def script(d: ReplDriver) -> None:
        await d.say("hello")
        await d.until(lambda: "[a1b2c3d4] done" in d.output)

    out = run(script, host).output
    assert "Background answer." in out
    assert {call[1] for call in host.stream_calls} == {"r-1", PARENT}


def test_ctrl_z_moves_the_request_to_the_background() -> None:
    host = FakeHost()
    host.request_ids = [PARENT]
    gate = Gate()
    host.script(PARENT, trace("chat.start"), gate, chat_end("response", text="Late answer."),
                completed("Late answer."))

    async def script(d: ReplDriver) -> None:
        d.type("long task" + ENTER)
        await d.until(lambda: d.repl.running() and d.repl.foreground.cursor == 1)
        d.type(CTRL_Z)
        await d.until(lambda: "moved to the background" in d.output)
        assert not d.repl.running()
        gate.open()
        await d.until(lambda: "[a1b2c3d4] done" in d.output)

    out = run(script, host).output
    assert host.modes == [("primary", PARENT, "bg")]
    # The watcher goes on from the event the foreground stopped at.
    assert host.stream_calls == [("primary", PARENT, 0), ("primary", PARENT, 1)]
    assert "Late answer." in out
    assert host.cancelled == []


def test_ctrl_z_sends_the_next_queued_message() -> None:
    host = FakeHost()
    host.script("r-1", trace("chat.start"), Gate())
    host.script("r-2", chat_end("response", text="Second."), completed("Second."))

    async def script(d: ReplDriver) -> None:
        d.type("one" + ENTER)
        await d.until(d.repl.running)
        d.type("two" + ENTER)
        await d.until(lambda: d.repl.queue == ["two"])
        d.type(CTRL_Z)
        await d.until(lambda: len(host.submitted) == 2)
        await d.until(lambda: "Second." in d.output)

    run(script, host)
    assert [s["message"] for s in host.submitted] == ["one", "two"]


def test_a_request_that_finished_meanwhile_still_returns_to_the_prompt() -> None:
    host = FakeHost()
    host.request_ids = [PARENT]
    host.script(PARENT, trace("chat.start"), Gate())

    def refuse(agent_id: str, request_id: str, mode: str) -> dict[str, Any]:
        raise MashHostClientError("request is completed", status_code=409)

    host.set_request_mode = refuse  # type: ignore[method-assign]

    async def script(d: ReplDriver) -> None:
        d.type("long task" + ENTER)
        await d.until(d.repl.running)
        d.type(CTRL_Z)
        await d.until(lambda: "could not move to the background" in d.output)
        assert not d.repl.running()

    out = run(script, host).output
    assert "[a1b2c3d4] could not move to the background: request is completed" in out
    assert "moved to the background" not in out


def test_ctrl_z_with_nothing_running_does_nothing() -> None:
    host = FakeHost()
    host.script("r-1", chat_end("response", text="hi"), completed("hi"))

    async def script(d: ReplDriver) -> None:
        d.type(CTRL_Z)
        await asyncio.sleep(0.05)
        await d.say("hello")

    out = run(script, host).output
    assert host.modes == []
    assert "hi" in out
