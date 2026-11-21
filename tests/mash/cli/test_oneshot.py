"""``mash run``: one request, its answer on stdout, an exit status."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from mash.cli.client import MashHostClientError
from mash.cli.oneshot import (
    EXIT_FAILED,
    EXIT_NEEDS_INPUT,
    EXIT_OK,
    read_message,
    run_once,
)
from tests.mash.cli.repl_harness import (
    FakeHost,
    chat_end,
    completed,
    failed,
    interaction,
    tool_end,
    tool_start,
)


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def _run(host: FakeHost, *, err: io.StringIO | None = None, **kwargs: Any) -> tuple[int, str, str]:
    out, err = io.StringIO(), err or io.StringIO()
    code = run_once(
        host,  # type: ignore[arg-type]
        **{
            "agent_id": "primary",
            "host_id": None,
            "session_id": "s-1",
            "user_id": "u-1",
            "message": "hello",
            "stdout": out,
            "stderr": err,
            **kwargs,
        },
    )
    return code, out.getvalue(), err.getvalue()


def _answer(host: FakeHost, text: str = "The **answer**.") -> None:
    host.script(
        "r-1",
        tool_start("t1", "search", {"q": "x"}),
        tool_end("t1", "search"),
        chat_end("response", text=text),
        completed(text),
    )


def test_the_answer_is_printed_as_is_and_nothing_else_reaches_stdout() -> None:
    host = FakeHost()
    _answer(host)
    code, out, err = _run(host)
    assert (code, out, err) == (EXIT_OK, "The **answer**.\n", "")
    assert host.submitted == [
        {"agent_id": "primary", "message": "hello", "session_id": "s-1",
         "user_id": "u-1", "request_id": "r-1"}
    ]


def test_progress_goes_to_stderr_when_it_is_a_terminal() -> None:
    host = FakeHost()
    _answer(host)
    code, out, err = _run(host, err=_Terminal())
    assert code == EXIT_OK
    assert out == "The **answer**.\n"
    assert "✓" in err and "search(q=x)" in err


def test_a_host_target_submits_through_the_host() -> None:
    host = FakeHost()
    _answer(host)
    code, _, _ = _run(host, agent_id="companion", host_id="fudge")
    assert code == EXIT_OK
    assert host.submitted[0]["host_id"] == "fudge"
    assert host.stream_calls == [("companion", "r-1", 0)]


def test_json_prints_one_object_per_event() -> None:
    host = FakeHost()
    _answer(host)
    code, out, err = _run(host, json_output=True)
    events = [json.loads(line) for line in out.splitlines()]
    assert code == EXIT_OK and err == ""
    assert [event["event"] for event in events] == [
        "agent.trace", "agent.trace", "agent.trace", "request.completed"
    ]
    assert events[-1]["data"]["response"]["text"] == "The **answer**."
    assert [event["seq"] for event in events] == [1, 2, 3, 4]


def test_a_failed_request_exits_1_with_its_error_on_stderr() -> None:
    host = FakeHost()
    host.script("r-1", failed("model overloaded"))
    assert _run(host) == (EXIT_FAILED, "", "model overloaded\n")


def test_a_host_that_refuses_the_request_exits_1() -> None:
    host = FakeHost()

    def refuse(*args: Any, **kwargs: Any) -> str:
        raise MashHostClientError("agent is disabled", status_code=409)

    host.submit_request = refuse  # type: ignore[method-assign]
    assert _run(host) == (EXIT_FAILED, "", "agent is disabled\n")


def test_a_request_that_asks_for_input_is_cancelled_and_exits_2() -> None:
    host = FakeHost()
    host.script("r-1", interaction("i-1", "approval", "Approve execution of: deploy?"),
                completed("never"))
    code, out, err = _run(host)
    assert code == EXIT_NEEDS_INPUT
    assert out == ""
    assert "Approve execution of: deploy?" in err
    assert host.cancelled == [("primary", "r-1")]
    assert host.answers == []


def test_the_message_is_the_argument_or_stdin() -> None:
    assert read_message(" hi there ", _Terminal()) == "hi there"
    assert read_message(None, io.StringIO("from a pipe\n")) == "from a pipe"
    assert read_message("", io.StringIO("line one\nline two\n")) == "line one\nline two"
    with pytest.raises(ValueError, match="argument or on stdin"):
        read_message(None, _Terminal())
    with pytest.raises(ValueError, match="empty"):
        read_message(None, io.StringIO("  \n"))
