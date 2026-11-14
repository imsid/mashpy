"""The REPL loop, run on a pipe against a scripted host."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from mash.cli.client import MashHostClientError
from tests.mash.cli.repl_harness import (
    CTRL_C,
    CTRL_D,
    CTRL_O,
    DOWN,
    ENTER,
    UP,
    FakeHost,
    Gate,
    ReplDriver,
    ack,
    cancelled,
    chat_end,
    completed,
    delta,
    failed,
    interaction,
    run_repl,
    tool_end,
    tool_start,
    trace,
)


def run(script, host: FakeHost, **kwargs: Any) -> ReplDriver:
    return asyncio.run(run_repl(script, host, **kwargs))


def simple(text: str = "Hello there.") -> list[Any]:
    return [chat_end("response", text=text), completed(text, session_id="s-1")]


# --- sending a message -----------------------------------------------------


def test_a_message_is_submitted_for_the_user_and_its_answer_printed() -> None:
    host = FakeHost()
    host.script("r-1", *simple("Hello there."))

    async def script(d: ReplDriver) -> None:
        await d.say("hello")

    d = run(script, host)
    assert host.submitted == [
        {"agent_id": "primary", "message": "hello", "session_id": "s-1",
         "user_id": "u-1", "mode": "fg", "request_id": "r-1"}
    ]
    assert "Hello there." in d.output
    assert d.repl.context.last_request_id == "r-1"
    assert host.stream_calls == [("primary", "r-1", 0)]


def test_a_host_target_submits_through_the_host_and_follows_the_companion() -> None:
    host = FakeHost()
    host.script("r-1", *simple())

    async def script(d: ReplDriver) -> None:
        await d.say("hello")

    d = run(script, host, host_id="fudge")
    assert host.submitted[0]["host_id"] == "fudge"
    assert host.stream_calls == [("companion", "r-1", 0)]
    assert d.repl.context.agent_id == "companion"
    assert "host fudge" in d.output.splitlines()[0]


def test_a_multi_line_message_is_sent_whole() -> None:
    host = FakeHost()
    host.script("r-1", *simple())

    async def script(d: ReplDriver) -> None:
        await d.say("first line\x1b\rsecond line")

    run(script, host)
    assert host.submitted[0]["message"] == "first line\nsecond line"


def test_the_session_id_of_the_completed_request_is_adopted() -> None:
    host = FakeHost()
    host.script("r-1", chat_end("response", text="hi"), completed("hi", session_id="s-new"))
    host.script("r-2", chat_end("response", text="again"), completed("again"))

    async def script(d: ReplDriver) -> None:
        await d.say("one")
        await d.say("two")

    d = run(script, host)
    assert d.repl.context.session_id == "s-new"
    assert host.submitted[1]["session_id"] == "s-new"


def test_a_streamed_answer_is_printed_once() -> None:
    host = FakeHost()
    host.script(
        "r-1",
        trace("chat.start"),
        delta("Streamed **answer**.\n\n"),
        delta("Second paragraph."),
        chat_end("response", text="Streamed **answer**.\n\nSecond paragraph."),
        completed("Streamed **answer**.\n\nSecond paragraph."),
    )

    async def script(d: ReplDriver) -> None:
        await d.say("hello")

    d = run(script, host)
    assert d.output.count("Streamed answer.") == 1
    assert d.output.count("Second paragraph.") == 1


def test_an_answer_that_did_not_stream_is_printed_when_the_request_ends() -> None:
    host = FakeHost()
    host.script("r-1", chat_end("response", text="Whole answer."), completed("Whole answer."))

    async def script(d: ReplDriver) -> None:
        await d.say("hello")

    assert run(script, host).output.count("Whole answer.") == 1


def test_a_request_with_no_text_says_so() -> None:
    host = FakeHost()
    host.script("r-1", chat_end("response"), completed(""))

    async def script(d: ReplDriver) -> None:
        await d.say("hello")

    assert "(no response)" in run(script, host).output


def test_tool_calls_print_one_line_each_before_the_answer() -> None:
    host = FakeHost()
    host.script(
        "r-1",
        chat_end("tool_call", tool_calls=[{"id": "t1", "name": "search", "arguments": {"q": "x"}}]),
        tool_start("t1", "search", {"q": "x"}),
        tool_end("t1", "search", duration_ms=2500),
        chat_end("response", text="Found it."),
        completed("Found it."),
    )

    async def script(d: ReplDriver) -> None:
        await d.say("find x")

    lines = [line.rstrip() for line in run(script, host).output.splitlines()]
    assert lines.index("  ✓ search(q=x) 2.5s") < lines.index("Found it.")


def test_the_apps_tool_lines_replace_the_preview() -> None:
    host = FakeHost()
    host.script(
        "r-1",
        tool_start("t1", "search", {"q": "x"}),
        tool_end("t1", "search"),
        *simple(),
    )

    async def script(d: ReplDriver) -> None:
        await d.say("find x")

    d = run(script, host, tool_lines={"search": lambda args: f"Searching for {args['q']}"})
    assert "  ✓ Searching for x" in d.output


def test_a_failed_request_prints_its_error_and_the_repl_goes_on() -> None:
    host = FakeHost()
    host.script("r-1", failed("model overloaded"))
    host.script("r-2", *simple("Second worked."))

    async def script(d: ReplDriver) -> None:
        await d.say("one")
        await d.say("two")

    out = run(script, host).output
    assert "✗ model overloaded" in out
    assert "Second worked." in out


def test_a_stream_that_breaks_is_reported_and_the_repl_goes_on() -> None:
    host = FakeHost()
    host.script("r-1", trace("chat.start"), MashHostClientError("connection lost"))
    host.script("r-2", *simple("Still here."))

    async def script(d: ReplDriver) -> None:
        await d.say("one")
        await d.say("two")

    out = run(script, host).output
    assert "✗ connection lost" in out
    assert "Still here." in out


def test_a_submit_that_fails_is_reported() -> None:
    host = FakeHost()

    def refuse(*args: Any, **kwargs: Any) -> str:
        raise MashHostClientError("agent is disabled", status_code=409)

    host.submit_request = refuse  # type: ignore[method-assign]

    async def script(d: ReplDriver) -> None:
        d.type("hello" + ENTER)
        await d.until(lambda: "agent is disabled" in d.output)

    assert "Error: agent is disabled" in run(script, host).output


# --- detail level ----------------------------------------------------------


def test_the_debug_flag_starts_at_the_detail_level() -> None:
    host = FakeHost()
    host.script(
        "r-1",
        chat_end("response", text="Hi.", input_tokens=1200, output_tokens=40, duration_ms=900),
        completed("Hi."),
    )

    async def script(d: ReplDriver) -> None:
        await d.say("hello")

    out = run(script, host, debug=True).output
    assert "step 1 · response · 1.2k in / 40 out · 0.9s" in out
    assert "1 step · 0 tools · 1.2k tokens" in out


def test_ctrl_o_switches_the_detail_level() -> None:
    host = FakeHost()
    host.script("r-1", chat_end("response", text="One.", input_tokens=10, output_tokens=1), completed("One."))
    host.script("r-2", chat_end("response", text="Two.", input_tokens=10, output_tokens=1), completed("Two."))

    async def script(d: ReplDriver) -> None:
        await d.say("one")
        assert "step 1" not in d.output
        d.type(CTRL_O)
        await d.until(lambda: d.repl.detail)
        await d.say("two")

    assert "step 1 · response" in run(script, host).output


# --- cancel, exit, queue ---------------------------------------------------


def test_ctrl_c_cancels_the_running_request() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", trace("chat.start"), gate, cancelled())

    async def script(d: ReplDriver) -> None:
        d.type("long task" + ENTER)
        await d.until(d.repl.running)
        d.type(CTRL_C)
        await d.until(lambda: host.cancelled == [("primary", "r-1")])
        gate.open()
        await d.idle()

    out = run(script, host).output
    assert "Request cancelled." in out
    assert "Bye." in out


def test_ctrl_c_with_text_typed_clears_the_input_and_cancels_nothing() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", trace("chat.start"), gate, *simple())
    host.script("r-2", *simple("Second."))

    async def script(d: ReplDriver) -> None:
        d.type("long task" + ENTER)
        await d.until(d.repl.running)
        d.type("half a thought" + CTRL_C)
        await asyncio.sleep(0.1)
        assert host.cancelled == []
        gate.open()
        await d.idle()
        # The cleared text is gone: only what is typed now is sent.
        await d.say("next")

    run(script, host)
    assert [s["message"] for s in host.submitted] == ["long task", "next"]


def test_ctrl_c_twice_at_an_idle_prompt_leaves() -> None:
    host = FakeHost()

    async def script(d: ReplDriver) -> None:
        d.type(CTRL_C)
        await asyncio.sleep(0.1)
        assert not d.task.done()
        d.type(CTRL_C)
        await asyncio.wait_for(d.task, timeout=5)

    assert "Bye." in run(script, host).output


def test_a_message_typed_while_a_request_runs_is_sent_after_it() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", trace("chat.start"), gate, *simple("First."))
    host.script("r-2", *simple("Second."))

    async def script(d: ReplDriver) -> None:
        d.type("one" + ENTER)
        await d.until(d.repl.running)
        d.type("two" + ENTER)
        await d.until(lambda: d.repl.queue == ["two"])
        assert len(host.submitted) == 1
        gate.open()
        await d.until(lambda: len(host.submitted) == 2)
        await d.idle()

    out = run(script, host).output
    assert [s["message"] for s in host.submitted] == ["one", "two"]
    assert out.index("First.") < out.index("Second.")


def test_up_on_an_empty_input_takes_the_last_queued_message_back() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", trace("chat.start"), gate, *simple())
    host.script("r-2", *simple())

    async def script(d: ReplDriver) -> None:
        d.type("one" + ENTER)
        await d.until(d.repl.running)
        d.type("two" + ENTER)
        await d.until(lambda: d.repl.queue == ["two"])
        d.type(UP)
        await d.until(lambda: d.repl.queue == [])
        d.type(" and three" + ENTER)
        await d.until(lambda: d.repl.queue == ["two and three"])
        gate.open()
        await d.until(lambda: len(host.submitted) == 2)
        await d.idle()

    run(script, host)
    assert host.submitted[1]["message"] == "two and three"


def test_leaving_while_a_request_runs_says_it_keeps_running() -> None:
    host = FakeHost()
    host.script("abcdef123456", trace("chat.start"), Gate())
    host.request_ids = ["abcdef123456"]

    async def script(d: ReplDriver) -> None:
        d.type("long task" + ENTER)
        await d.until(d.repl.running)
        d.type(CTRL_D)
        await asyncio.wait_for(d.task, timeout=5)

    out = run(script, host).output
    assert "[abcdef12] is still running on the host." in out
    assert host.cancelled == []


# --- slash commands ---------------------------------------------------------


def test_a_slash_command_runs_and_an_unknown_one_is_named() -> None:
    host = FakeHost()

    async def script(d: ReplDriver) -> None:
        await d.say("/help")
        await d.say("/nope")

    out = run(script, host).output
    assert "/feedback" in out
    assert "Unknown command: /nope" in out
    assert host.submitted == []


def test_exit_leaves_the_repl() -> None:
    host = FakeHost()

    async def script(d: ReplDriver) -> None:
        d.type("/exit" + ENTER)
        await asyncio.wait_for(d.task, timeout=5)

    assert "Bye." in run(script, host).output


def test_a_command_that_raises_is_reported() -> None:
    host = FakeHost()

    def boom(agent_id: str, session_id: str) -> dict[str, Any]:
        raise MashHostClientError("no such session")

    async def script(d: ReplDriver) -> None:
        with patch.object(host, "get_session", boom):
            await d.say("/session")

    assert "Command failed: no such session" in run(script, host).output


# --- children ----------------------------------------------------------------


def _companion_script(host: FakeHost, *, energy: list[Any], review: list[Any]) -> None:
    host.script(
        "r-1",
        chat_end("tool_call", tool_calls=[{"id": "t1", "name": "RunSubAgents", "arguments": {}}]),
        tool_start(
            "t1", "RunSubAgents",
            {"requests": [{"agent_id": "energy", "prompt": "usage"},
                          {"agent_id": "review", "prompt": "bills"}]},
            subagents=[{"agent_id": "energy", "request_id": "c-0"},
                       {"agent_id": "review", "request_id": "c-1"}],
        ),
        Gate(),
        tool_end("t1", "RunSubAgents"),
        chat_end("response", text="Both done."),
        completed("Both done."),
    )
    host.script("c-0", *energy)
    host.script("c-1", *review)


def _parent_gate(host: FakeHost) -> Gate:
    return next(item for item in host.scripts["r-1"] if isinstance(item, Gate))


def test_children_are_followed_at_the_same_time() -> None:
    host = FakeHost()
    energy_gate, review_gate = Gate(), Gate()
    _companion_script(
        host,
        energy=[tool_start("x", "run_code"), energy_gate, tool_end("x", "run_code"), completed("e")],
        review=[review_gate, completed("r")],
    )

    async def script(d: ReplDriver) -> None:
        d.type("why was march high" + ENTER)
        # Both children's streams are open while neither has ended.
        await d.until(lambda: {call[1] for call in host.stream_calls} == {"r-1", "c-0", "c-1"})
        view = d.repl.foreground
        assert [child.agent_id for child in view.children] == ["energy", "review"]
        await d.until(lambda: view.children[0].current_tool() is not None)
        assert not view.children[0].ended and not view.children[1].ended
        # The second child ends first, and is reported first.
        review_gate.open()
        await d.until(lambda: view.children[1].ended)
        assert not view.children[0].ended
        energy_gate.open()
        await d.until(lambda: view.children[0].ended)
        _parent_gate(host).open()
        await d.idle()

    lines = [line.rstrip() for line in run(script, host, host_id="fudge").output.splitlines()]
    asked = lines.index("  ● Asking 2 agents: energy, review")
    review_line = next(i for i, line in enumerate(lines) if line.startswith("    ✓ review · 0 tools"))
    energy_line = next(i for i, line in enumerate(lines) if line.startswith("    ✓ energy · 1 tool"))
    assert asked < review_line < energy_line < lines.index("Both done.")
    # At the default level a child's own tool lines are not printed.
    assert not any("run_code" in line for line in lines)


def test_a_child_listed_before_it_is_submitted_is_waited_for() -> None:
    host = FakeHost()
    _companion_script(host, energy=[completed("e")], review=[completed("r")])
    host.missing = {"c-0"}

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(lambda: ("review", "c-1", 0) in host.stream_calls)
        assert ("energy", "c-0", 0) not in host.stream_calls
        host.missing = set()
        await d.until(lambda: ("energy", "c-0", 0) in host.stream_calls)
        _parent_gate(host).open()
        await d.idle()

    out = run(script, host, host_id="fudge").output
    assert "✓ energy" in out and "✓ review" in out


def test_a_childs_broken_stream_does_not_end_the_parent() -> None:
    host = FakeHost()
    _companion_script(
        host,
        energy=[trace("chat.start"), MashHostClientError("Response ended prematurely")],
        review=[completed("r")],
    )

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(lambda: "Response ended prematurely" in d.output)
        _parent_gate(host).open()
        await d.idle()

    out = run(script, host, host_id="fudge").output
    assert "✗ energy · 0 tools" in out
    assert "failed: Response ended prematurely" in out
    assert "Both done." in out


def test_a_failed_child_prints_its_error() -> None:
    host = FakeHost()
    _companion_script(host, energy=[failed("tool exploded")], review=[completed("r")])

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(lambda: "tool exploded" in d.output)
        _parent_gate(host).open()
        await d.idle()

    assert "failed: tool exploded" in run(script, host, host_id="fudge").output


def test_the_detail_level_prints_each_childs_tools() -> None:
    host = FakeHost()
    _companion_script(
        host,
        energy=[tool_start("x", "run_code", {"language": "shell", "code": "ls"}),
                tool_end("x", "run_code"), completed("e")],
        review=[completed("r")],
    )

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(lambda: "energy │" in d.output)
        _parent_gate(host).open()
        await d.idle()

    out = run(script, host, host_id="fudge", debug=True).output
    assert "  energy │ ✓ $ ls" in out
    assert "    energy usage" in out


def _three_children(host: FakeHost, gates: list[Gate]) -> None:
    names = ["energy", "review", "solar"]
    host.script(
        "r-1",
        tool_start(
            "t1", "RunSubAgents",
            {"requests": [{"agent_id": name, "prompt": f"look at {name}"} for name in names[:2]]
             + [{"agent_id": "solar", "prompt": "look at solar", "session_id": "s-old"}]},
            subagents=[{"agent_id": name, "request_id": f"c-{i}"} for i, name in enumerate(names)],
        ),
        Gate(),
        tool_end("t1", "RunSubAgents"),
        chat_end("response", text="All three done."),
        completed("All three done."),
    )
    for index, gate in enumerate(gates):
        host.script(
            f"c-{index}",
            tool_start("x", "search", {"q": names[index]}),
            tool_end("x", "search"),
            gate,
            chat_end("response", text=f"{names[index]} answer", input_tokens=100, output_tokens=5),
            completed(f"{names[index]} answer"),
        )


def test_three_children_print_three_lines_in_the_order_they_end() -> None:
    host = FakeHost()
    gates = [Gate(), Gate(), Gate()]
    _three_children(host, gates)

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(lambda: d.repl.foreground is not None
                      and len(d.repl.foreground.children) == 3
                      and all(c.tool_count == 1 for c in d.repl.foreground.children))
        children = d.repl.foreground.children
        for index in (2, 0, 1):
            gates[index].open()
            await d.until(lambda: children[index].ended)
        _parent_gate(host).open()
        await d.idle()

    lines = [line.strip() for line in run(script, host, host_id="fudge").output.splitlines()]
    ended = [line.split()[1] for line in lines if line.startswith("✓ ") and "· 1 tool ·" in line]
    assert ended == ["solar", "energy", "review"]
    # A child that continues an earlier session is marked.
    assert any(line.startswith("✓ solar ↺") for line in lines)


def test_subagent_lists_shows_and_cancels_children() -> None:
    host = FakeHost()
    gates = [Gate(), Gate(), Gate()]
    _three_children(host, gates)

    async def script(d: ReplDriver) -> None:
        await d.say("/subagent")
        assert "No request in this REPL has started subagents yet." in d.output
        d.type("go" + ENTER)
        await d.until(lambda: d.repl.foreground is not None
                      and len(d.repl.foreground.children) == 3)
        children = d.repl.foreground.children
        gates[0].open()
        await d.until(lambda: children[0].ended)
        # One child is cancelled while the request runs; the others go on.
        await d.command("/subagent cancel 2")
        assert host.cancelled == [("review", "c-1")]
        assert not children[1].ended and not children[2].ended
        await d.command("/subagent cancel 1")
        await d.command("/subagent 9")
        await d.command("/subagent cancel")
        gates[1].open()
        gates[2].open()
        await d.until(lambda: all(child.ended for child in children))
        _parent_gate(host).open()
        await d.idle()
        await d.say("/subagent")
        await d.say("/subagent 1")

    out = run(script, host, host_id="fudge").output
    assert "energy already ended (completed)." in out
    assert "Give a number from 1 to 3; /subagent lists them." in out
    assert "Usage: /subagent | /subagent <n> | /subagent cancel <n>" in out
    assert host.cancelled == [("review", "c-1")]
    # The list: one row per child, with what it was asked.
    table = out[out.index("Asked"):]
    for name in ("energy", "review", "solar ↺"):
        assert name in table
    assert "look at review" in table
    # Showing a child replays its own stream: its brief, steps, tools, answer.
    shown = out[out.rindex("energy was asked:"):]
    assert "look at energy" in shown
    assert "step 1 · response · 100 in / 5 out" in shown
    assert "✓ search(q=energy)" in shown
    assert "energy answer" in shown
    assert host.stream_calls.count(("energy", "c-0", 0)) == 2


# --- workflows started by the companion ---------------------------------------


def _workflow_script(host: FakeHost, run_events: list[Any]) -> Gate:
    gate = Gate()
    host.script(
        "r-1",
        tool_start(
            "t1", "RunWorkflow", {"workflow_id": "export", "input": {}},
            workflow_run={"workflow_id": "export", "dedup_key": "r-1-0-0"},
        ),
        gate,
        tool_end("t1", "RunWorkflow"),
        chat_end("response", text="Exported."),
        completed("Exported."),
    )
    host.workflow_runs["export"] = [
        {"run_id": "run-old", "dedup_key": "other"},
        {"run_id": "run-7", "dedup_key": "r-1-0-0"},
    ]
    host.workflow_scripts["run-7"] = run_events
    return gate


def _step(name: str, step_id: str) -> dict[str, Any]:
    return {"event": name, "data": {"step_id": step_id}}


def test_a_workflow_calls_row_shows_the_step_its_run_is_on() -> None:
    host = FakeHost()
    step_gate = Gate()
    gate = _workflow_script(
        host,
        [_step("step.started", "scan"), _step("step.completed", "scan"),
         _step("step.started", "summarize"), step_gate,
         _step("step.completed", "summarize"), {"event": "workflow.completed", "data": {}}],
    )

    async def script(d: ReplDriver) -> None:
        d.type("export it" + ENTER)
        await d.until(lambda: d.repl.running() and d.repl.foreground.current_tool() is not None
                      and d.repl.foreground.current_tool().note == "step summarize")
        rows = "".join(text for _, text in d.repl._message())
        assert "RunWorkflow(workflow_id=export, input={}) · step summarize" in rows
        step_gate.open()
        gate.open()
        await d.idle()

    out = run(script, host, debug=True).output
    assert ("export", "run-7", 0) in host.stream_calls
    # The detail level prints each step of the run as it ends.
    assert "export │ ✓ step scan" in out
    assert "Exported." in out


def test_a_workflow_run_that_cannot_be_read_leaves_the_request_alone() -> None:
    host = FakeHost()
    gate = _workflow_script(host, [MashHostClientError("stream broke")])

    async def script(d: ReplDriver) -> None:
        d.type("export it" + ENTER)
        await d.until(lambda: ("export", "run-7", 0) in host.stream_calls)
        gate.open()
        await d.idle()

    out = run(script, host).output
    assert "stream broke" not in out
    assert "Exported." in out


def test_a_workflow_run_is_not_followed_after_its_call_ends() -> None:
    host = FakeHost()
    gate = _workflow_script(host, [_step("step.started", "scan"), Gate()])

    async def script(d: ReplDriver) -> None:
        d.type("export it" + ENTER)
        await d.until(lambda: d.repl.running() and d.repl.foreground.current_tool() is not None
                      and d.repl.foreground.current_tool().note == "step scan")
        gate.open()
        await d.idle()
        # The run's stream is still open on the host; the REPL let go of it.
        assert d.repl._workflow_tasks == {}
        assert not d.repl._child_tasks

    assert "Exported." in run(script, host).output


# --- sessions -----------------------------------------------------------------


def _sessions(host: FakeHost) -> None:
    import time as _time

    host.sessions = [
        {"session_id": "s-bills", "first_message": "why was march high",
         "turn_count": 3, "last_activity_at": _time.time() - 7200},
        {"session_id": "s-solar", "first_message": "compare solar quotes",
         "turn_count": 1, "last_activity_at": _time.time() - 3 * 86400},
    ]
    host.history["s-solar"] = [
        {"user_message": "compare solar quotes", "agent_response": "Quote B is cheaper."},
    ]
    host.history["s-bills"] = [
        {"user_message": "first", "agent_response": "one"},
        {"user_message": "why was march high", "agent_response": "Heating."},
        {"user_message": "and april?", "agent_response": "Lower."},
    ]


def test_a_resumed_session_starts_with_its_last_two_turns() -> None:
    host = FakeHost()
    _sessions(host)
    host.script("r-1", *simple())

    async def script(d: ReplDriver) -> None:
        await d.say("go on")

    d = run(script, host, target={"session_id": "s-bills", "resumed": True,
                                  "title": "why was march high"})
    lines = [line.rstrip() for line in d.output.splitlines()]
    assert lines[0] == "mash · u-1 · agent primary · why was march high · /help for commands"
    assert lines[1:5] == ["› why was march high", "Heating.", "› and april?", "Lower."]
    assert "› first" not in lines
    assert host.submitted[0]["session_id"] == "s-bills"


def test_resume_picks_one_of_the_users_sessions() -> None:
    host = FakeHost()
    _sessions(host)
    host.script("r-1", chat_end("response", text="ok"), completed("ok"))

    async def script(d: ReplDriver) -> None:
        d.type("/resume" + ENTER)
        await d.until(lambda: ("list_sessions", ("primary", "u-1", 20)) in host.calls)
        await asyncio.sleep(0.05)
        d.type(DOWN + ENTER)
        await d.until(lambda: d.repl.context.session_id == "s-solar")
        await d.until(lambda: "Quote B is cheaper." in d.output)
        await d.say("go on")

    d = run(script, host)
    assert "· compare solar quotes ·" in d.output
    assert "› compare solar quotes" in d.output
    assert host.submitted[0]["session_id"] == "s-solar"


def test_resume_can_be_left_with_ctrl_c_and_says_when_there_is_nothing() -> None:
    host = FakeHost()

    async def script(d: ReplDriver) -> None:
        await d.say("/resume")
        _sessions(host)
        d.type("/resume" + ENTER)
        await d.until(lambda: len([c for c in host.calls if c[0] == "list_sessions"]) == 2)
        await asyncio.sleep(0.05)
        d.type(CTRL_C)
        await d.until(lambda: "Kept the current session." in d.output)

    d = run(script, host)
    assert "No earlier sessions." in d.output
    assert d.repl.context.session_id == "s-1"


def test_new_starts_a_new_session() -> None:
    host = FakeHost()
    host.script("r-1", *simple())
    host.script("r-2", chat_end("response", text="ok"), completed("ok"))

    async def script(d: ReplDriver) -> None:
        await d.say("one")
        await d.say("/new")
        await d.say("two")

    d = run(script, host)
    assert "New session." in d.output
    first, second = (s["session_id"] for s in host.submitted)
    assert first == "s-1" and second not in ("s-1", "")
    assert d.repl.context.session_id == second


def test_the_session_cannot_change_while_a_request_runs() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", trace("chat.start"), gate, *simple())

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(d.repl.running)
        await d.command("/new")
        assert d.repl.context.session_id == "s-1"
        gate.open()
        await d.idle()

    assert "A request is running in the foreground." in run(script, host).output


# --- interactions -------------------------------------------------------------


def _asking(d: ReplDriver):
    return lambda: d.repl._asking is not None


def _approval(host: FakeHost, *before: Any) -> Gate:
    gate = Gate()
    host.script(
        "r-1",
        *before,
        interaction("i-1", "approval", "Approve execution of: deploy?"),
        gate,
        ack("i-1", "approve"),
        *simple("Deployed."),
    )
    return gate


def _answer_approval(keys: str, *before: Any) -> tuple[FakeHost, str]:
    host = FakeHost()
    gate = _approval(host, *before)

    async def script(d: ReplDriver) -> None:
        d.type("deploy it" + ENTER)
        await d.until(_asking(d))
        d.type(keys)
        await d.until(lambda: len(host.answers) == 1)
        gate.open()
        await d.idle()

    return host, run(script, host).output


def test_enter_approves_and_the_answer_is_posted() -> None:
    host, out = _answer_approval(ENTER)
    assert host.answers == [
        {"agent_id": "primary", "request_id": "r-1", "interaction_id": "i-1", "response": "approve"}
    ]
    lines = [line.rstrip() for line in out.splitlines()]
    assert lines.index("Approve execution of: deploy?") < lines.index("  → approve")
    assert "Deployed." in out


def test_an_option_is_picked_by_arrow_number_or_first_letter() -> None:
    assert _answer_approval(DOWN + ENTER)[0].answers[0]["response"] == "deny"
    assert _answer_approval("3" + ENTER)[0].answers[0]["response"] == "skip"
    assert _answer_approval("d" + ENTER)[0].answers[0]["response"] == "deny"


def test_an_approval_shows_the_call_and_can_show_all_of_it() -> None:
    call = {"id": "t1", "name": "deploy", "arguments": {"env": "prod", "notes": "x" * 300}}
    other = {"id": "t2", "name": "search", "arguments": {"q": "y"}}
    host, out = _answer_approval(
        "4" + ENTER + ENTER, chat_end("tool_call", tool_calls=[call, other])
    )
    lines = [line.rstrip() for line in out.splitlines()]
    # The call the approval names is shown under the question, clipped.
    shown = lines[lines.index("Approve execution of: deploy?") + 1]
    assert shown.startswith("    deploy(env=prod")
    assert "x" * 300 not in shown
    assert not any(line.startswith("    search(") for line in lines)
    # "View the full call" prints every argument and asks again.
    assert '"env": "prod"' in out
    assert "x" * 60 in out
    assert host.answers[0]["response"] == "approve"


def test_a_choice_answers_with_the_checked_options() -> None:
    host = FakeHost()
    gate = Gate()
    host.script(
        "r-1",
        interaction("i-1", "choice", "Which months?",
                    schema={"type": "multi_select", "options": ["jan", "feb", "mar"]}),
        gate,
        *simple(),
    )

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(_asking(d))
        d.type(" " + DOWN + DOWN + " " + ENTER)
        await d.until(lambda: len(host.answers) == 1)
        gate.open()
        await d.idle()

    out = run(script, host).output
    assert host.answers[0]["response"] == ["jan", "mar"]
    assert "  → jan, mar" in out


def test_a_choice_with_nothing_checked_answers_with_the_highlighted_option() -> None:
    host = FakeHost()
    gate = Gate()
    host.script(
        "r-1",
        interaction("i-1", "choice", "Which month?",
                    schema={"type": "multi_select", "options": ["jan", "feb"]}),
        gate,
        *simple(),
    )

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(_asking(d))
        d.type(DOWN + ENTER)
        await d.until(lambda: len(host.answers) == 1)
        gate.open()
        await d.idle()

    run(script, host)
    assert host.answers[0]["response"] == ["feb"]


def test_a_question_takes_free_text() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", interaction("i-1", "info", "Which account?", schema={"type": "text"}),
                gate, *simple())

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(_asking(d))
        d.type("the joint one" + ENTER)
        await d.until(lambda: len(host.answers) == 1)
        gate.open()
        await d.idle()

    run(script, host)
    assert host.answers[0]["response"] == "the joint one"


def _browser(keys: str) -> tuple[FakeHost, str, str]:
    host = FakeHost()
    gate = Gate()
    url = "http://127.0.0.1:8000/browser/s/" + "t" * 120 + "/vnc.html"
    host.script("r-1", interaction("i-1", "browser", "Sign in to PG&E", url=url), gate, *simple())

    async def script(d: ReplDriver) -> None:
        with patch("mash.cli.repl.webbrowser.open") as opened:
            d.type("go" + ENTER)
            await d.until(_asking(d))
            opened.assert_called_once_with(url)
            d.type(keys)
            await d.until(lambda: len(host.answers) == 1)
        gate.open()
        await d.idle()

    return host, run(script, host).output, url


def test_a_browser_handoff_prints_and_opens_the_link_then_answers_done() -> None:
    host, out, url = _browser(ENTER)
    assert host.answers[0]["response"] == "done"
    # The link is never broken across lines.
    assert f"  Open: {url}" in out.splitlines()


def test_a_browser_handoff_can_be_cancelled() -> None:
    assert _browser(DOWN + ENTER)[0].answers[0]["response"] == "cancel"


def test_a_childs_interaction_is_asked_through_the_parent_and_names_the_child() -> None:
    host = FakeHost()
    child_gate = Gate()
    _companion_script(
        host,
        energy=[
            chat_end("tool_call", tool_calls=[
                {"id": "x", "name": "run_code", "arguments": {"language": "shell", "code": "rm -r out"}}
            ]),
            interaction("ci-1", "approval", "Approve execution of: run_code?"),
            child_gate, ack("ci-1", "approve"), completed("e"),
        ],
        review=[completed("r")],
    )
    parent_gate = _parent_gate(host)
    index = host.scripts["r-1"].index(parent_gate)
    hold, relayed_gate = Gate(), Gate()
    host.scripts["r-1"][index:index] = [
        hold,
        interaction(
            "relay-1", "approval", "Approve execution of: run_code?",
            origin={"agent_id": "energy", "request_id": "c-0", "interaction_id": "ci-1"},
        ),
        relayed_gate,
        ack("relay-1", "approve"),
    ]

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(lambda: d.repl.foreground is not None
                      and len(d.repl.foreground.children) == 2
                      and d.repl.foreground.children[0].pending_tool_calls)
        hold.open()
        await d.until(_asking(d))
        d.type(ENTER)
        await d.until(lambda: len(host.answers) == 1)
        child_gate.open()
        relayed_gate.open()
        parent_gate.open()
        await d.idle()

    out = run(script, host, host_id="fudge").output
    # The answer goes to the companion's request, under the relayed id; the
    # child's own copy is not asked a second time.
    assert host.answers == [
        {"agent_id": "companion", "request_id": "r-1",
         "interaction_id": "relay-1", "response": "approve"}
    ]
    assert out.count("Approve execution of: run_code?") == 1
    lines = [line.rstrip() for line in out.splitlines()]
    asked = lines.index("[energy] Approve execution of: run_code?")
    # The child's call is shown, not the companion's RunSubAgents call.
    assert lines[asked + 1] == "    $ rm -r out"
    assert "✓ energy" in out


def test_ctrl_c_at_an_interaction_cancels_the_request() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", interaction("i-1", "approval", "Approve?"), gate, cancelled())

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(_asking(d))
        d.type(CTRL_C)
        await d.until(lambda: host.cancelled == [("primary", "r-1")])
        gate.open()
        await d.idle()

    out = run(script, host).output
    assert host.answers == []
    assert "Cancelling the request." in out
    assert "Request cancelled." in out


def test_ctrl_c_at_a_question_cancels_the_request() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", interaction("i-1", "info", "Name?"), gate, cancelled())

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(_asking(d))
        d.type("si" + CTRL_C)
        await d.until(lambda: host.cancelled == [("primary", "r-1")])
        gate.open()
        await d.idle()

    assert run(script, host).host.answers == []


def test_ctrl_d_at_an_interaction_leaves_the_repl() -> None:
    host = FakeHost()
    host.script("r-1", interaction("i-1", "approval", "Approve?"), Gate())

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(_asking(d))
        d.type(CTRL_D)
        await asyncio.wait_for(d.task, timeout=5)

    out = run(script, host).output
    assert host.answers == [] and host.cancelled == []
    assert "is still running on the host." in out


def test_a_menu_closes_when_its_interaction_is_answered_elsewhere() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", interaction("i-1", "approval", "Approve?"), gate,
                ack("i-1", "deny"), *simple("Went on."))

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(_asking(d))
        gate.open()
        await d.idle()
        assert d.repl._asking is None

    out = run(script, host).output
    assert "No longer waiting for an answer." in out
    assert "Went on." in out
    assert host.answers == []


def test_a_question_closes_when_its_request_ends() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", interaction("i-1", "info", "Name?"), gate, failed("timed out"))
    host.script("r-2", *simple("Next."))

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(_asking(d))
        gate.open()
        await d.idle()
        await d.say("next")

    out = run(script, host).output
    assert "No longer waiting for an answer." in out
    assert "Next." in out


def test_the_question_says_how_many_more_are_waiting() -> None:
    host = FakeHost()
    gate = Gate()
    host.script(
        "r-1",
        interaction("i-1", "approval", "Approve execution of: a?"),
        interaction("i-2", "approval", "Approve execution of: b?"),
        gate,
        *simple(),
    )

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(lambda: d.repl._asking is not None and len(d.repl._interactions) == 1)
        d.type(ENTER)
        await d.until(lambda: len(host.answers) == 1)
        await d.until(_asking(d))
        d.type(DOWN + ENTER)
        await d.until(lambda: len(host.answers) == 2)
        gate.open()
        await d.idle()

    out = run(script, host).output
    assert [a["response"] for a in host.answers] == ["approve", "deny"]
    assert "Approve execution of: b?" in out


def test_a_draft_survives_an_interaction_that_arrives_while_typing() -> None:
    host = FakeHost()
    start, gate = Gate(), Gate()
    host.script("r-1", trace("chat.start"), start,
                interaction("i-1", "approval", "Approve?"), gate, *simple())
    host.script("r-2", *simple())

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(d.repl.running)
        d.type("my next thou")
        await asyncio.sleep(0.1)
        start.open()
        await d.until(_asking(d))
        d.type(ENTER)
        await d.until(lambda: len(host.answers) == 1)
        gate.open()
        await d.idle()
        # The draft is back in the input; finish it and send.
        d.type("ght" + ENTER)
        await d.until(lambda: len(host.submitted) == 2)
        await d.idle()

    run(script, host)
    assert host.submitted[1]["message"] == "my next thought"


def test_an_interaction_that_times_out_before_it_is_asked_is_dropped() -> None:
    host = FakeHost()
    host.script(
        "r-1",
        interaction("i-1", "approval", "Approve?"),
        ack("i-1", "deny", timed_out=True),
        *simple("Went on without it."),
    )

    async def script(d: ReplDriver) -> None:
        await d.say("go")

    out = run(script, host).output
    assert "Interaction i-1 timed out" in out
    assert "Went on without it." in out
    assert host.answers == []


def test_an_answer_the_host_refuses_is_reported() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", interaction("i-1", "info", "Name?"), gate, *simple())

    def closed(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise MashHostClientError("interaction 'i-1' is no longer open", status_code=409)

    host.post_interaction = closed  # type: ignore[method-assign]

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(_asking(d))
        d.type("sid" + ENTER)
        await d.until(lambda: "was not accepted" in d.output)
        gate.open()
        await d.idle()

    assert "no longer open" in run(script, host).output
