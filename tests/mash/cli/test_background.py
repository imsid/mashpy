"""Background requests in the REPL: /bg, Ctrl-Z, and idle watching."""

from __future__ import annotations

import os
import signal
import threading
import time
import unittest
from typing import Any, cast
from unittest.mock import patch

from mash.cli.background import (
    BG_USAGE,
    BgCommand,
    RequestWatcher,
    list_rows,
    parse_bg_args,
    resolve_prefix,
)
from mash.cli.client import MashHostClientError
from mash.cli.shell import MashRemoteShell, ShellTarget


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


class ParseTests(unittest.TestCase):
    def test_the_four_forms(self) -> None:
        self.assertEqual(
            parse_bg_args(["set", "a", "timer"]),
            BgCommand(action="start", message="set a timer"),
        )
        self.assertEqual(parse_bg_args(["--list"]), BgCommand(action="list"))
        self.assertEqual(
            parse_bg_args(["--switch", "a1b2"]), BgCommand(action="switch", request_id="a1b2")
        )
        self.assertEqual(
            parse_bg_args(["--kill", "a1b2"]), BgCommand(action="kill", request_id="a1b2")
        )

    def test_a_message_that_starts_with_a_flag_word_is_a_message(self) -> None:
        self.assertEqual(
            parse_bg_args(["list", "my", "files"]),
            BgCommand(action="start", message="list my files"),
        )

    def test_usage_errors(self) -> None:
        for args in ([], ["--list", "x"], ["--switch"], ["--kill", "a", "b"], ["--later"]):
            with self.subTest(args=args):
                self.assertEqual(parse_bg_args(args), BG_USAGE)


class PrefixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [_record(PARENT), _record(OTHER), _record(CHILD_1)]

    def test_a_unique_prefix_resolves(self) -> None:
        self.assertEqual(resolve_prefix("c1", self.records)["request_id"], CHILD_1)
        self.assertEqual(resolve_prefix("a1b2c3", self.records)["request_id"], PARENT)

    def test_an_ambiguous_prefix_is_an_error_message(self) -> None:
        message = resolve_prefix("a1b2", self.records)
        self.assertIsInstance(message, str)
        self.assertIn("matches 2 requests", message)
        self.assertIn("a1b2c3d4", message)

    def test_an_unknown_prefix_is_an_error_message(self) -> None:
        self.assertEqual(
            resolve_prefix("zz", self.records), "No background request matches 'zz'."
        )


class ListRowsTests(unittest.TestCase):
    def test_children_are_indented_under_their_parent(self) -> None:
        records = [
            _record(CHILD_2, parent_request_id=PARENT, started_by="primary", started_at=3.0),
            _record(CHILD_1, parent_request_id=PARENT, started_by="primary", started_at=2.0),
            _record(OTHER, status="completed", started_at=1.5),
            _record(PARENT, started_at=1.0),
        ]
        self.assertEqual(
            [row[0] for row in list_rows(records)],
            ["a1b2ffff", "a1b2c3d4", "  └ c1c1c1c1", "  └ c2c2c2c2"],
        )
        self.assertEqual(list_rows(records)[0][2:], ["completed", "user"])
        self.assertEqual(list_rows(records)[2][3], "primary")

    def test_a_child_of_an_unlisted_parent_is_shown_at_the_top(self) -> None:
        rows = list_rows([_record(CHILD_1, parent_request_id="fg-parent")])
        self.assertEqual(rows[0][0], "c1c1c1c1")


class _StreamClient:
    def __init__(self, events: list[dict[str, Any]], *, hold: float = 0.0) -> None:
        self.events = events
        self.hold = hold

    def stream_request(self, agent_id: str, request_id: str):
        del agent_id, request_id
        for event in self.events:
            yield event
        if self.hold:
            time.sleep(self.hold)


class WatcherTests(unittest.TestCase):
    def _watch(self, events: list[dict[str, Any]], *, hold: float = 0.0) -> list[list]:
        notices: list[list] = []
        watcher = RequestWatcher(
            _StreamClient(events, hold=hold), "primary", PARENT, notices.append, input_delay=0.05
        )
        watcher.start()
        watcher.join(5)
        return notices

    def test_completed_prints_done_and_the_response(self) -> None:
        notices = self._watch(
            [{"event": "request.completed", "data": {"response": {"text": "Timer is up."}}}]
        )
        self.assertEqual(notices, [[("info", "[a1b2c3d4] done"), ("markdown", "Timer is up.")]])

    def test_failed_and_cancelled(self) -> None:
        self.assertEqual(
            self._watch([{"event": "request.error", "data": {"error": "boom"}}]),
            [[("error", "[a1b2c3d4] failed: boom")]],
        )
        self.assertEqual(
            self._watch([{"event": "request.cancelled", "data": {}}]),
            [[("warn", "[a1b2c3d4] cancelled")]],
        )

    def test_an_open_interaction_asks_for_input(self) -> None:
        notices = self._watch(
            [{"event": "request.interaction.create", "data": {"interaction_id": "i-1"}}],
            hold=0.3,
        )
        self.assertEqual(
            notices,
            [[("warn", "[a1b2c3d4] needs your input: /bg --switch a1b2c3d4")]],
        )

    def test_an_answered_interaction_in_the_replay_is_not_announced(self) -> None:
        notices = self._watch(
            [
                {"event": "request.interaction.create", "data": {"interaction_id": "i-1"}},
                {"event": "request.interaction.ack", "data": {"interaction_id": "i-1"}},
                {"event": "agent.trace", "data": {}},
            ],
            hold=0.3,
        )
        self.assertEqual(notices, [])

    def test_a_silenced_watcher_prints_nothing(self) -> None:
        notices: list[list] = []
        gate = threading.Event()

        class _Gated:
            def stream_request(self, agent_id, request_id):
                gate.wait(5)
                yield {"event": "request.completed", "data": {"response": {"text": "x"}}}

        watcher = RequestWatcher(_Gated(), "primary", PARENT, notices.append)
        watcher.start()
        watcher.silence()
        gate.set()
        watcher.join(5)
        self.assertEqual(notices, [])


class _ShellClient:
    """A fake host client for the shell's /bg flows."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.submitted: list[dict[str, Any]] = []
        self.modes: list[tuple[str, str, str]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.stream: Any = None
        self.mode_error: Exception | None = None

    def submit_request(self, agent_id, *, message, session_id, user_id, mode="fg"):
        self.submitted.append({"agent_id": agent_id, "message": message, "mode": mode})
        return PARENT

    def submit_host_request(self, host_id, *, message, session_id, user_id, mode="fg"):
        self.submitted.append({"host_id": host_id, "message": message, "mode": mode})
        return {"request_id": PARENT, "agent_id": "primary"}

    def list_session_requests(self, agent_id, session_id, **filters):
        self.list_calls.append(filters)
        return [
            r
            for r in self.records
            if all(r.get(key) == value for key, value in filters.items())
        ]

    def set_request_mode(self, agent_id, request_id, mode):
        if self.mode_error is not None:
            raise self.mode_error
        self.modes.append((agent_id, request_id, mode))
        return {"request_id": request_id, "mode": mode}

    def cancel_request(self, agent_id, request_id):
        self.cancelled.append((agent_id, request_id))
        return {"status": "cancelled"}

    def stream_request(self, agent_id, request_id):
        yield from self.stream(agent_id, request_id)

    def post_interaction(self, *args, **kwargs):
        return {}


def _completes(text: str):
    def _stream(agent_id, request_id):
        del agent_id, request_id
        yield {"event": "request.completed", "data": {"response": {"text": text}}}

    return _stream


class BgCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _ShellClient()
        self.client.stream = _completes("ok")
        self.shell = self._shell()
        self.watched: list[tuple[str, str]] = []
        self.silenced: list[str] = []
        patch.object(
            self.shell.watchers, "watch", side_effect=lambda a, r: self.watched.append((a, r))
        ).start()
        patch.object(
            self.shell.watchers, "silence", side_effect=self.silenced.append
        ).start()
        self.addCleanup(patch.stopall)

    def _shell(self, host_id: str | None = None) -> MashRemoteShell:
        return MashRemoteShell(
            cast(Any, self.client),
            ShellTarget(
                api_base_url="http://localhost:8000",
                agent_id="primary",
                session_id="s-1",
                user_id="u-1",
                host_id=host_id,
            ),
        )

    def _run(self, line: str) -> None:
        self.shell.command_registry.execute(self.shell.context, line)

    def test_bg_message_submits_in_the_background_and_watches_it(self) -> None:
        with patch.object(self.shell.renderer, "info") as info:
            self._run("/bg set a timer for 1 min")
        self.assertEqual(
            self.client.submitted,
            [{"agent_id": "primary", "message": "set a timer for 1 min", "mode": "bg"}],
        )
        self.assertEqual(self.watched, [("primary", PARENT)])
        info.assert_called_with("\\[a1b2c3d4] running in the background")

    def test_bg_message_through_a_host(self) -> None:
        self.shell = self._shell(host_id="assistant")
        patch.object(self.shell.watchers, "watch").start()
        self._run("/bg list my files")
        self.assertEqual(
            self.client.submitted,
            [{"host_id": "assistant", "message": "list my files", "mode": "bg"}],
        )

    def test_list_shows_children_under_their_parent(self) -> None:
        self.client.records = [
            _record(CHILD_1, parent_request_id=PARENT, started_by="primary", started_at=2.0),
            _record(PARENT, started_at=1.0),
        ]
        with patch.object(self.shell.renderer, "table") as table:
            self._run("/bg --list")
        headers, rows = table.call_args.args
        self.assertEqual(headers, ["ID", "Message", "Status", "Started by"])
        self.assertEqual([row[0] for row in rows], ["a1b2c3d4", "  └ c1c1c1c1"])
        self.assertEqual(self.client.list_calls, [{"mode": "bg"}])

    def test_switch_brings_a_request_to_the_foreground_and_follows_it(self) -> None:
        self.client.records = [_record(PARENT)]
        self.client.stream = _completes("Timer is up.")
        with patch.object(self.shell.renderer, "markdown") as markdown:
            self._run("/bg --switch a1b2")
        self.assertEqual(self.client.modes, [("primary", PARENT, "fg")])
        self.assertEqual(self.silenced, [PARENT])
        markdown.assert_called_with("Timer is up.")

    def test_switch_to_a_child_leaves_its_mode(self) -> None:
        self.client.records = [
            _record(CHILD_1, agent_id="research", parent_request_id=PARENT, started_by="primary")
        ]
        streamed: list[tuple[str, str]] = []

        def _stream(agent_id, request_id):
            streamed.append((agent_id, request_id))
            yield {"event": "request.completed", "data": {"response": {"text": "r"}}}

        self.client.stream = _stream
        with patch.object(self.shell.renderer, "markdown"):
            self._run("/bg --switch c1")
        self.assertEqual(self.client.modes, [])
        self.assertEqual(streamed, [("research", CHILD_1)])

    def test_switch_and_kill_report_a_bad_prefix(self) -> None:
        self.client.records = [_record(PARENT), _record(OTHER)]
        with patch.object(self.shell.renderer, "error") as error:
            self._run("/bg --switch a1b2")
            self._run("/bg --kill zz")
        self.assertIn("matches 2 requests", error.call_args_list[0].args[0])
        self.assertEqual(
            error.call_args_list[1].args[0], "No background request matches 'zz'."
        )
        self.assertEqual(self.client.modes, [])
        self.assertEqual(self.client.cancelled, [])

    def test_kill_calls_the_cancel_route_on_the_requests_agent(self) -> None:
        self.client.records = [_record(CHILD_1, agent_id="research", parent_request_id=PARENT)]
        with patch.object(self.shell.renderer, "warn") as warn:
            self._run("/bg --kill c1c1")
        self.assertEqual(self.client.cancelled, [("research", CHILD_1)])
        self.assertEqual(self.silenced, [CHILD_1])
        warn.assert_called_with("\\[c1c1c1c1] cancelled")

    def test_a_usage_error_is_printed(self) -> None:
        with patch.object(self.shell.renderer, "error") as error:
            self._run("/bg")
        error.assert_called_once_with(BG_USAGE)

    def test_after_a_foreground_request_the_shell_watches_running_bg_requests(self) -> None:
        self.client.records = [
            _record(PARENT),
            _record(OTHER, status="completed"),
            _record(CHILD_1, parent_request_id=PARENT, started_by="primary"),
        ]
        with patch.object(self.shell.renderer, "markdown"):
            self.shell.handle_repl_message(self.shell.context, "hello")
        self.assertEqual(
            self.client.list_calls[-1],
            {"mode": "bg", "status": "running", "started_by": "user"},
        )
        self.assertEqual(self.watched, [("primary", PARENT)])


@unittest.skipUnless(hasattr(signal, "SIGTSTP"), "needs SIGTSTP")
class CtrlZTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _ShellClient()
        self.shell = MashRemoteShell(
            cast(Any, self.client),
            ShellTarget(
                api_base_url="http://localhost:8000",
                agent_id="primary",
                session_id="s-1",
                user_id="u-1",
            ),
        )
        self.watched: list[tuple[str, str]] = []
        patch.object(
            self.shell.watchers, "watch", side_effect=lambda a, r: self.watched.append((a, r))
        ).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def _ctrl_z_while_waiting(agent_id, request_id):
        """A stream that sends Ctrl-Z and then waits for an event that never comes."""
        del agent_id, request_id
        yield {
            "event": "agent.trace",
            "data": {"event_type": "mash.context_load.end", "payload": {}},
        }
        os.kill(os.getpid(), signal.SIGTSTP)
        time.sleep(5)
        yield {"event": "request.completed", "data": {"response": {"text": "late"}}}

    def test_ctrl_z_moves_the_request_to_the_background(self) -> None:
        self.client.stream = self._ctrl_z_while_waiting
        before = signal.getsignal(signal.SIGTSTP)
        started = time.monotonic()
        with patch.object(self.shell.renderer, "info") as info:
            outcome = self.shell.follow_request(self.shell.context, "primary", PARENT)

        self.assertEqual(outcome, "backgrounded")
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(self.client.modes, [("primary", PARENT, "bg")])
        self.assertEqual(self.watched, [("primary", PARENT)])
        info.assert_called_with("\\[a1b2c3d4] moved to the background")
        self.assertEqual(signal.getsignal(signal.SIGTSTP), before)

    def test_ctrl_z_on_a_child_leaves_its_mode(self) -> None:
        self.client.stream = self._ctrl_z_while_waiting
        with patch.object(self.shell.renderer, "info"):
            outcome = self.shell.follow_request(
                self.shell.context, "research", CHILD_1, child=True
            )
        self.assertEqual(outcome, "backgrounded")
        self.assertEqual(self.client.modes, [])
        self.assertEqual(self.watched, [])

    def test_a_request_that_finished_meanwhile_still_returns_to_the_prompt(self) -> None:
        self.client.stream = self._ctrl_z_while_waiting
        self.client.mode_error = MashHostClientError("409: request is completed")
        with patch.object(self.shell.renderer, "info"), patch.object(
            self.shell.renderer, "warn"
        ) as warn:
            outcome = self.shell.follow_request(self.shell.context, "primary", PARENT)
        self.assertEqual(outcome, "backgrounded")
        self.assertIn("could not move to the background", warn.call_args.args[0])

    def test_notices_wait_until_the_stream_read_ends(self) -> None:
        printed: list[str] = []

        def _stream(agent_id, request_id):
            del agent_id, request_id
            self.shell.notify([("info", "[ffffffff] done")])
            self.assertEqual(printed, [])
            yield {"event": "request.completed", "data": {"response": {"text": "ok"}}}

        self.client.stream = _stream
        with patch.object(self.shell.renderer, "info", side_effect=printed.append), patch.object(
            self.shell.renderer, "markdown"
        ):
            self.shell.follow_request(self.shell.context, "primary", PARENT)
        self.assertEqual(printed, ["\\[ffffffff] done"])



class RichOutputTests(unittest.TestCase):
    def test_short_ids_survive_rich_markup(self) -> None:
        from io import StringIO

        out = StringIO()
        shell = MashRemoteShell(
            cast(Any, _ShellClient()),
            ShellTarget(
                api_base_url="http://localhost:8000",
                agent_id="primary",
                session_id="s-1",
                user_id="u-1",
            ),
        )
        shell.renderer.console.file = out
        shell.notify([("info", "[a1b2c3d4] done"), ("warn", "[a1b2c3d4] cancelled")])
        self.assertIn("[a1b2c3d4] done", out.getvalue())
        self.assertIn("[a1b2c3d4] cancelled", out.getvalue())


if __name__ == "__main__":
    unittest.main()
