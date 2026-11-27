"""``reduce``: one stream event applied to a ``RequestView``."""

from __future__ import annotations

from typing import Any

from mash.cli.views import (
    CANCELLED,
    COMPLETED,
    FAILED,
    RUNNING,
    WAITING,
    Ack,
    Children,
    End,
    Interaction,
    RequestView,
    Step,
    Text,
    Tool,
    final_text,
    reduce,
    split_complete_markdown,
)

from tests.mash.cli.repl_harness import (
    ack,
    cancelled,
    chat_end,
    completed,
    delta,
    failed,
    interaction,
    tool_end,
    tool_start,
    trace,
)


def view() -> RequestView:
    return RequestView(agent_id="primary", request_id="r-1", started_at=100.0)


def apply(target: RequestView, *events: dict[str, Any], now: float = 101.0) -> list[Any]:
    outputs: list[Any] = []
    for event in events:
        outputs.extend(reduce(target, event, now))
    return outputs


def test_the_seq_of_each_event_becomes_the_cursor() -> None:
    v = view()
    reduce(v, {**trace("chat.start"), "seq": 7}, 101.0)
    assert v.cursor == 7
    reduce(v, trace("chat.start"), 101.0)
    assert v.cursor == 7


def test_accepted_records_the_session() -> None:
    v = view()
    apply(v, {"event": "request.accepted", "data": {"session_id": "s-9"}})
    assert v.session_id == "s-9"
    assert v.status == RUNNING


def test_chat_start_is_thinking() -> None:
    v = view()
    assert apply(v, trace("chat.start")) == []
    assert v.phase == "thinking"


def test_deltas_print_whole_markdown_blocks_and_keep_the_rest() -> None:
    v = view()
    assert apply(v, delta("First para")) == []
    assert v.phase == "answering"
    assert v.tail == "First para"
    outputs = apply(v, delta("graph.\n\nSecond"))
    assert outputs == [Text(v, "First paragraph.\n")]
    assert v.tail == "Second"


def test_a_delta_wrapped_by_the_logging_layer_is_read() -> None:
    v = view()
    apply(v, trace("chat.delta", {"payload": {"text": "hi\n\n"}}))
    assert v.step_streamed == "hi\n\n"


def test_an_open_code_fence_stays_buffered_until_it_closes() -> None:
    assert split_complete_markdown("intro\n\n```py\nx = 1\n\ny = 2") == ("intro\n", "```py\nx = 1\n\ny = 2")
    done, rest = split_complete_markdown("```py\nx = 1\n```\n\nafter")
    assert done == "```py\nx = 1\n```\n"
    assert rest == "after"
    assert split_complete_markdown("no boundary yet") == ("", "no boundary yet")


def test_a_block_prints_once_the_next_one_starts_at_the_margin() -> None:
    # A blank line alone is not a boundary yet: the next line may be indented.
    assert split_complete_markdown("intro\n\n") == ("", "intro\n\n")
    assert split_complete_markdown("intro\n\nNext") == ("intro\n", "Next")
    # An indented line after a blank line belongs to the list item above it.
    item = "1. First\n\n   More about the first\n\n   ```py\n   x = 1\n   ```"
    assert split_complete_markdown(item) == ("", item)
    done, rest = split_complete_markdown(item + "\n\n2. Second")
    assert done == item + "\n"
    assert rest == "2. Second"


def test_chat_end_flushes_the_tail_and_counts_tokens_and_steps() -> None:
    v = view()
    apply(v, delta("The answer."))
    outputs = apply(v, chat_end("response", text="The answer.", input_tokens=1200, output_tokens=30))
    assert outputs[0] == Text(v, "The answer.")
    step = outputs[1]
    assert isinstance(step, Step)
    assert (step.number, step.action, step.input_tokens, step.output_tokens) == (1, "response", 1200, 30)
    assert step.duration_ms == 100
    assert step.reasoning_tokens is None
    assert (v.steps, v.input_tokens, v.output_tokens, v.tokens) == (1, 1200, 30, 1230)
    assert v.tail == ""
    assert v.response_streamed == "The answer."


def test_chat_end_reports_reasoning_tokens_and_thinking() -> None:
    v = view()
    blocks = [
        {"type": "thinking", "thinking": "first thought"},
        {"type": "text", "text": "hi"},
        {"type": "thinking", "thinking": "second thought"},
    ]
    (step,) = apply(v, chat_end("response", blocks=blocks, reasoning=64))
    assert step.reasoning_tokens == 64
    assert step.thinking == "first thought\n\nsecond thought"


def test_a_tool_call_turn_keeps_its_calls_for_an_approval() -> None:
    v = view()
    calls = [{"id": "t1", "name": "deploy", "arguments": {"env": "prod"}}]
    (step,) = apply(v, chat_end("tool_call", tool_calls=calls))
    assert step.tool_names == ("deploy",)
    assert v.pending_tool_calls == calls
    assert v.phase == "tool"


def test_text_before_tool_calls_that_did_not_stream_is_a_preamble() -> None:
    v = view()
    calls = [{"id": "t1", "name": "search", "arguments": {}}]
    (step,) = apply(v, chat_end("tool_call", text="Let me look.", tool_calls=calls))
    assert step.preamble == "Let me look."

    streamed = view()
    apply(streamed, delta("Let me look."))
    outputs = apply(streamed, chat_end("tool_call", text="Let me look.", tool_calls=calls))
    assert outputs[0] == Text(streamed, "Let me look.")
    assert outputs[1].preamble == ""
    # Text streamed before tool calls is not the answer.
    assert streamed.response_streamed == ""


def test_a_tool_runs_from_its_start_to_its_end() -> None:
    v = view()
    assert apply(v, tool_start("t1", "search", {"query": "x"}), now=105.0) == []
    running = v.current_tool()
    assert running is not None
    assert (running.name, running.arguments, running.started_at) == ("search", {"query": "x"}, 105.0)
    assert v.phase == "tool"

    (output,) = apply(v, tool_end("t1", "search", content="3 rows", duration_ms=1500))
    assert isinstance(output, Tool)
    assert (output.call.name, output.call.duration_ms, output.call.is_error) == ("search", 1500, False)
    assert output.call.result == "3 rows"
    assert output.call.arguments == {"query": "x"}
    assert v.current_tool() is None
    assert v.tool_count == 1
    assert v.phase == "thinking"


def test_parallel_tools_end_independently() -> None:
    v = view()
    apply(v, tool_start("t1", "a"), now=101.0)
    apply(v, tool_start("t2", "b"), now=102.0)
    assert v.current_tool().name == "b"
    apply(v, tool_end("t2", "b"))
    assert v.current_tool().name == "a"
    assert v.phase == "tool"
    apply(v, tool_end("t1", "a"))
    assert v.tool_count == 2


def test_a_failed_tool_is_an_error_with_its_result() -> None:
    v = view()
    apply(v, tool_start("t1", "run_code"))
    (output,) = apply(v, tool_end("t1", "run_code", content="NameError: x", is_error=True))
    assert output.call.is_error
    assert output.call.result == "NameError: x"


def test_a_tool_end_without_its_start_still_counts() -> None:
    v = view()
    (output,) = apply(v, tool_end("t9", "search"))
    assert output.call.name == "search"
    assert v.tool_count == 1


def test_a_tool_result_made_of_blocks_is_read_as_text() -> None:
    v = view()
    event = tool_end("t1", "fetch")
    event["data"]["payload"]["result"]["content"] = [
        {"type": "text", "text": "one"},
        {"type": "image"},
        {"type": "text", "text": "two"},
    ]
    (output,) = apply(v, event)
    assert output.call.result == "one\ntwo"


def _run_subagents() -> dict[str, Any]:
    return tool_start(
        "t1",
        "RunSubAgents",
        {
            "requests": [
                {"agent_id": "energy", "prompt": "March usage by hour"},
                {"agent_id": "review", "prompt": "Bills in March", "session_id": "s-old"},
            ]
        },
        subagents=[
            {"agent_id": "energy", "request_id": "r-1-0"},
            {"agent_id": "review", "request_id": "r-1-1"},
        ],
    )


def test_run_subagents_lists_its_children_with_their_briefs() -> None:
    v = view()
    (output,) = apply(v, _run_subagents(), now=110.0)
    assert isinstance(output, Children)
    energy, review = output.children
    assert (energy.agent_id, energy.request_id, energy.brief) == ("energy", "r-1-0", "March usage by hour")
    assert (review.agent_id, review.request_id, review.brief) == ("review", "r-1-1", "Bills in March")
    assert energy.parent is v and review.parent is v
    assert (energy.continued, review.continued) == (False, True)
    assert energy.started_at == 110.0
    assert v.children == [energy, review]


def test_totals_include_the_children() -> None:
    v = view()
    (output,) = apply(v, _run_subagents())
    energy, review = output.children
    apply(v, chat_end("response", input_tokens=100, output_tokens=10))
    apply(energy, chat_end("response", input_tokens=1000, output_tokens=50))
    apply(energy, tool_start("c1", "run_code"), tool_end("c1", "run_code"))
    apply(review, chat_end("response", input_tokens=500, output_tokens=5))
    assert v.total_tokens() == 110 + 1050 + 505
    assert v.total_tools() == 1


def test_an_interaction_waits_and_its_ack_resumes() -> None:
    v = view()
    apply(v, delta("Before asking"))
    outputs = apply(v, interaction("i-1", "approval", "Approve execution of: deploy?"))
    assert outputs[0] == Text(v, "Before asking")
    assert isinstance(outputs[1], Interaction)
    assert outputs[1].payload["interaction_id"] == "i-1"
    assert (v.status, v.phase) == (WAITING, "waiting")

    (output,) = apply(v, ack("i-1", "approve"))
    assert isinstance(output, Ack)
    assert output.payload["response"] == "approve"
    assert v.status == RUNNING


def test_the_terminal_events_end_the_request() -> None:
    done = view()
    apply(done, tool_start("t1", "search"))
    outputs = apply(done, completed("hi", session_id="s-2"), now=130.0)
    assert outputs == [End(done)]
    assert (done.status, done.ended_at, done.session_id) == (COMPLETED, 130.0, "s-2")
    assert done.ended and done.running_tools == {}
    assert done.elapsed(now=999.0) == 30.0

    broken = view()
    apply(broken, failed("model overloaded"))
    assert (broken.status, broken.error) == (FAILED, "model overloaded")

    stopped = view()
    apply(stopped, cancelled())
    assert stopped.status == CANCELLED


def test_final_text_is_empty_when_the_answer_streamed() -> None:
    v = view()
    apply(v, delta("Streamed answer."), chat_end("response", text="Streamed answer."))
    apply(v, completed("Streamed answer."))
    assert final_text(v) == ""


def test_final_text_is_the_response_when_nothing_streamed() -> None:
    v = view()
    apply(v, chat_end("response", text="Whole answer."), completed("Whole answer."))
    assert final_text(v) == "Whole answer."


def test_final_text_joins_text_blocks_and_skips_thinking() -> None:
    v = view()
    event = completed("ignored")
    event["data"]["response"] = {
        "text": "ignored",
        "assistant_blocks": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "One."},
            {"type": "text", "text": "Two."},
        ],
    }
    apply(v, event)
    assert final_text(v) == "One.\n\nTwo."


def test_final_text_falls_back_to_the_plain_text_field() -> None:
    v = view()
    apply(v, {"event": "request.completed", "data": {"text": "plain"}})
    assert final_text(v) == "plain"


def test_after_the_answer_the_request_is_finishing() -> None:
    view = RequestView(agent_id="a", request_id="r", started_at=0.0)
    reduce(view, chat_end("tool_call", tool_calls=[{"id": "t", "name": "x", "arguments": {}}]), 1.0)
    assert view.phase == "tool"
    reduce(view, chat_end("response", text="Done."), 2.0)
    assert view.phase == "finishing"
