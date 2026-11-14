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


# --- interactions -------------------------------------------------------------


def test_an_approval_is_asked_and_the_answer_posted() -> None:
    host = FakeHost()
    gate = Gate()
    host.script(
        "r-1",
        interaction("i-1", "approval", "Approve execution of: deploy?"),
        gate,
        ack("i-1", "approve"),
        *simple("Deployed."),
    )

    async def script(d: ReplDriver) -> None:
        d.type("deploy it" + ENTER)
        await d.until(lambda: "Approve execution of: deploy?" in d.output)
        d.type("approve" + ENTER)
        await d.until(lambda: len(host.answers) == 1)
        gate.open()
        await d.idle()

    out = run(script, host).output
    assert host.answers == [
        {"agent_id": "primary", "request_id": "r-1", "interaction_id": "i-1", "response": "approve"}
    ]
    assert "Deployed." in out


def test_an_answer_that_is_not_an_option_is_asked_again() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", interaction("i-1", "approval", "Approve?"), gate, *simple())

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(lambda: "Approve?" in d.output)
        d.type("aprove" + ENTER)
        await d.until(lambda: d.output.count("Options: approve, deny, skip") == 2)
        assert host.answers == []
        d.type("deny" + ENTER)
        await d.until(lambda: len(host.answers) == 1)
        gate.open()
        await d.idle()

    run(script, host)
    assert host.answers[0]["response"] == "deny"


def test_a_choice_takes_numbers_and_rejects_one_out_of_range() -> None:
    host = FakeHost()
    gate = Gate()
    host.script(
        "r-1",
        interaction("i-1", "choice", "Which months?", schema={"options": ["jan", "feb", "mar"]}),
        gate,
        *simple(),
    )

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(lambda: "3. mar" in d.output)
        d.type("1, 7" + ENTER)
        await d.until(lambda: d.output.count("Enter numbers separated by commas") == 2)
        d.type("1, 3" + ENTER)
        await d.until(lambda: len(host.answers) == 1)
        gate.open()
        await d.idle()

    run(script, host)
    assert host.answers[0]["response"] == ["jan", "mar"]


def test_a_question_takes_free_text() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", interaction("i-1", "question", "Which account?"), gate, *simple())

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(lambda: "Which account?" in d.output)
        d.type("the joint one" + ENTER)
        await d.until(lambda: len(host.answers) == 1)
        gate.open()
        await d.idle()

    run(script, host)
    assert host.answers[0]["response"] == "the joint one"


def test_a_browser_handoff_prints_and_opens_the_link_then_answers_done() -> None:
    host = FakeHost()
    gate = Gate()
    url = "http://127.0.0.1:8000/browser/s/" + "t" * 120 + "/vnc.html"
    host.script("r-1", interaction("i-1", "browser", "Sign in to PG&E", url=url), gate, *simple())

    async def script(d: ReplDriver) -> None:
        with patch("mash.cli.repl.webbrowser.open") as opened:
            d.type("go" + ENTER)
            await d.until(lambda: "Press Enter when you're done" in d.output)
            d.type(ENTER)
            await d.until(lambda: len(host.answers) == 1)
            opened.assert_called_once_with(url)
        gate.open()
        await d.idle()

    out = run(script, host).output
    assert host.answers[0]["response"] == "done"
    # The link is never broken across lines.
    assert f"  Open: {url}" in out.splitlines()


def test_a_browser_handoff_can_be_cancelled() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", interaction("i-1", "browser", "Sign in", url="http://x"), gate, *simple())

    async def script(d: ReplDriver) -> None:
        with patch("mash.cli.repl.webbrowser.open"):
            d.type("go" + ENTER)
            await d.until(lambda: "Press Enter" in d.output)
            d.type("cancel" + ENTER)
            await d.until(lambda: len(host.answers) == 1)
        gate.open()
        await d.idle()

    run(script, host)
    assert host.answers[0]["response"] == "cancel"


def test_a_childs_interaction_is_asked_through_the_parent_and_names_the_child() -> None:
    host = FakeHost()
    child_gate = Gate()
    _companion_script(
        host,
        energy=[interaction("ci-1", "approval", "Approve execution of: run_code?"),
                child_gate, ack("ci-1", "approve"), completed("e")],
        review=[completed("r")],
    )
    parent_gate = _parent_gate(host)
    index = host.scripts["r-1"].index(parent_gate)
    relayed_gate = Gate()
    host.scripts["r-1"][index:index] = [
        interaction(
            "relay-1", "approval", "Approve execution of: run_code?",
            origin={"agent_id": "energy", "request_id": "c-0", "interaction_id": "ci-1"},
        ),
        relayed_gate,
        ack("relay-1", "approve"),
    ]

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(lambda: "[energy] Approve execution of: run_code?" in d.output)
        d.type("approve" + ENTER)
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
    assert "✓ energy" in out


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
        await d.until(lambda: "Approve?" in d.output)
        d.type("approve" + ENTER)
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
    host.script("r-1", interaction("i-1", "question", "Name?"), gate, *simple())

    def closed(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise MashHostClientError("interaction 'i-1' is no longer open", status_code=409)

    host.post_interaction = closed  # type: ignore[method-assign]

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(lambda: "Name?" in d.output)
        d.type("sid" + ENTER)
        await d.until(lambda: "was not accepted" in d.output)
        gate.open()
        await d.idle()

    assert "no longer open" in run(script, host).output
