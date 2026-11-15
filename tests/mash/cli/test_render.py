"""Finished output (rich) and the live area and status bar (prompt_toolkit)."""

from __future__ import annotations

import io
from typing import Any

from rich.console import Console

from mash.cli.render import (
    OutputPrinter,
    RichRenderer,
    clip,
    fmt_seconds,
    fmt_tokens,
    live_rows,
    prompt_style_rules,
    rich_theme,
    status_bar,
    tool_display,
)
from mash.cli.views import RequestView, ToolCall, reduce
from tests.mash.cli.repl_harness import (
    chat_end,
    completed,
    delta,
    failed,
    interaction,
    tool_end,
    tool_start,
)


def renderer(width: int = 80, **kwargs: Any) -> tuple[RichRenderer, io.StringIO]:
    buffer = io.StringIO()
    console = Console(
        file=buffer, width=width, force_terminal=False, color_system=None,
        theme=rich_theme(), **kwargs,
    )
    return RichRenderer(console), buffer


def printed(*events: dict[str, Any], detail: bool = False, tool_lines=None, view=None) -> str:
    """What the printer writes for a request's events."""
    out, buffer = renderer(100)
    printer = OutputPrinter(out, detail=lambda: detail, tool_lines=tool_lines)
    view = view or RequestView(agent_id="primary", request_id="r-1", started_at=0.0)
    for event in events:
        for output in reduce(view, event, 10.0):
            printer.print(output, 10.0)
    return buffer.getvalue()


def text_of(fragments: list[tuple[str, str]]) -> str:
    return "".join(text for _, text in fragments)


def test_token_and_time_formats() -> None:
    assert [fmt_tokens(n) for n in (0, 950, 4100, 9999, 18_400, 1_200_000)] == [
        "0", "950", "4.1k", "10.0k", "18k", "1.2M",
    ]
    assert [fmt_seconds(s) for s in (0.84, 9.99, 12.7, 65, 3605)] == [
        "0.8s", "10.0s", "12s", "1m05s", "60m05s",
    ]


def test_clip_keeps_one_line_within_the_width() -> None:
    assert clip("one\n  two   three", 40) == "one two three"
    assert clip("abcdefghij", 6) == "abcde…"
    assert clip("abc", 0) == ""


def test_markdown_has_nothing_drawn_around_it() -> None:
    out, buffer = renderer(60)
    out.markdown("Some **bold** text.")
    assert buffer.getvalue().strip() == "Some bold text."
    out.markdown("   ")
    assert buffer.getvalue().strip() == "Some bold text."


def test_code_blocks_print_without_padding() -> None:
    out, buffer = renderer(60)
    out.markdown("```python\nx = 1\nprint(x)\n```")
    assert buffer.getvalue().splitlines() == ["x = 1", "print(x)"]


def test_markdown_links_are_terminal_hyperlinks(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    buffer = io.StringIO()
    console = Console(file=buffer, width=60, force_terminal=True, theme=rich_theme())
    RichRenderer(console).markdown("[the report](https://host/files/report.html)")
    assert "\x1b]8;" in buffer.getvalue()
    assert "https://host/files/report.html" in buffer.getvalue()


def test_blocks_are_set_apart_by_one_blank_line_without_padding() -> None:
    out, buffer = renderer(60)
    out.echo("summarize", "› ", "  ")
    out.markdown("# Summary")
    out.markdown("- one\n- two")
    out.markdown("> a quote")
    out.markdown("| a | b |\n|---|---|\n| 1 | 2 |")
    out.markdown("Done.")
    text = buffer.getvalue()
    assert "\n\n\n" not in text
    assert all(line == line.rstrip() for line in text.splitlines())
    assert text.startswith("› summarize\n\nSummary\n\n • one\n • two\n\n▌ a quote\n\n")
    assert text.endswith("\n\nDone.\n")


def test_tool_lines_after_an_answer_are_set_apart_and_kept_together() -> None:
    out, buffer = renderer(60)
    out.markdown("Looking.")
    out.print("  ✓ one")
    out.print("  ✓ two")
    out.print()
    out.print()
    assert buffer.getvalue() == "Looking.\n\n  ✓ one\n  ✓ two\n\n"


def test_headings_print_on_the_left() -> None:
    out, buffer = renderer(60)
    out.markdown("# Title")
    assert buffer.getvalue() == "Title\n"


def test_the_live_area_draws_markdown_as_it_will_print_without_hyperlinks(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    console = Console(file=io.StringIO(), width=60, force_terminal=True, theme=rich_theme())
    live = RichRenderer(console).render_markdown("See [the report](https://host/r.html)", 60)
    assert "the report" in live
    assert "\x1b]8;" not in live


def test_a_table_wraps_its_last_column_instead_of_clipping_it() -> None:
    out, buffer = renderer(50)
    sentence = "Run code in the user's sandbox and see everything it prints."
    out.table(["Tool", "Does"], [["run_code", sentence]])
    text = " ".join(buffer.getvalue().split())
    assert sentence in text
    assert "…" not in text


def test_link_stays_on_one_line_past_console_width() -> None:
    out, buffer = renderer(40)
    url = "http://127.0.0.1:8000/browser/s/" + "t" * 80 + "/vnc.html?autoconnect=1"
    out.link(f"  Open: {url}")
    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    assert lines == [f"  Open: {url}"]


def test_a_table_prints_its_headers_and_rows() -> None:
    out, buffer = renderer(60)
    out.table(["Agent", "Name"], [["energy", "Energy"], ["review", "Review"]])
    text = buffer.getvalue()
    assert "Agent" in text and "energy" in text and "Review" in text


def test_tool_display_uses_the_preview_or_the_apps_own_line() -> None:
    call = ToolCall("t1", "search", {"query": "pge march"}, started_at=0.0)
    assert tool_display(call) == "search(query=pge march)"
    code = ToolCall("t2", "run_code", {"language": "shell", "code": "ls -la\npwd"}, 0.0)
    assert tool_display(code) == "$ ls -la ..."
    lines = {"search": lambda args: f"Searching for {args['query']}"}
    assert tool_display(call, lines) == "Searching for pge march"
    # A line function that fails or returns nothing falls back to the preview.
    assert tool_display(call, {"search": lambda args: args["missing"]}) == "search(query=pge march)"
    assert tool_display(call, {"search": lambda args: ""}) == "search(query=pge march)"


def test_default_level_prints_one_line_per_tool_and_the_answer() -> None:
    text = printed(
        chat_end("tool_call", tool_calls=[{"id": "t1", "name": "search", "arguments": {"q": "x"}}],
                 input_tokens=1200, output_tokens=40),
        tool_start("t1", "search", {"q": "x"}),
        tool_end("t1", "search", content="3 rows", duration_ms=1500),
        delta("The answer.\n\n"),
        chat_end("response", text="The answer."),
    )
    assert text.splitlines()[0] == "  ✓ search(q=x) 1.5s"
    assert "The answer." in text
    assert "step" not in text and "1.2k" not in text and "3 rows" not in text


def test_a_fast_tool_shows_no_duration() -> None:
    text = printed(tool_start("t1", "search"), tool_end("t1", "search", duration_ms=40))
    assert text == "  ✓ search\n"


def test_a_failed_tool_shows_the_first_line_of_its_error() -> None:
    text = printed(
        tool_start("t1", "run_code", {"language": "python", "code": "x"}),
        tool_end("t1", "run_code", content="\nNameError: name 'x' is not defined\n  File ...", is_error=True),
    )
    assert text.splitlines() == [
        "  ✗ python: x",
        "    NameError: name 'x' is not defined",
    ]


def test_detail_level_adds_steps_tokens_thinking_and_results() -> None:
    text = printed(
        chat_end(
            "tool_call",
            tool_calls=[{"id": "t1", "name": "search", "arguments": {}}],
            blocks=[{"type": "thinking", "thinking": "I should search."}],
            input_tokens=1200, output_tokens=40, reasoning=64, duration_ms=812,
        ),
        tool_start("t1", "search"),
        tool_end("t1", "search", content="3 rows"),
        detail=True,
    )
    lines = text.splitlines()
    assert lines[0] == "  step 1 · search · 1.2k in / 40 out / 64 reasoning · 0.8s"
    assert lines[1].strip() == "I should search."
    assert lines[2] == "  ✓ search"
    assert lines[3] == "    3 rows"


def test_reasoning_tokens_are_omitted_when_the_provider_reports_none() -> None:
    text = printed(chat_end("response", input_tokens=10, output_tokens=5), detail=True)
    assert "reasoning" not in text


def test_text_before_tool_calls_that_did_not_stream_is_printed() -> None:
    text = printed(
        chat_end("tool_call", text="Let me look.",
                 tool_calls=[{"id": "t1", "name": "search", "arguments": {}}])
    )
    assert text.strip() == "Let me look."


def _companion() -> tuple[RequestView, RequestView, RequestView]:
    parent = RequestView(agent_id="companion", request_id="r-1", started_at=0.0)
    (output,) = reduce(
        parent,
        tool_start(
            "t1", "RunSubAgents",
            {"requests": [
                {"agent_id": "energy", "prompt": "March usage by hour"},
                {"agent_id": "review", "prompt": "Bills in March", "session_id": "s-old"},
            ]},
            subagents=[
                {"agent_id": "energy", "request_id": "c-0"},
                {"agent_id": "review", "request_id": "c-1"},
            ],
        ),
        0.0,
    )
    energy, review = output.children
    return parent, energy, review


def test_children_are_announced_and_each_ends_on_one_line() -> None:
    out, buffer = renderer(100)
    printer = OutputPrinter(out)
    parent = RequestView(agent_id="companion", request_id="r-1", started_at=0.0)
    start = tool_start(
        "t1", "RunSubAgents",
        {"requests": [{"agent_id": "energy", "prompt": "a"},
                      {"agent_id": "review", "prompt": "b", "session_id": "s-old"}]},
        subagents=[{"agent_id": "energy", "request_id": "c-0"},
                   {"agent_id": "review", "request_id": "c-1"}],
    )
    (children,) = reduce(parent, start, 0.0)
    printer.print(children, 0.0)
    energy, review = children.children
    for event in (
        chat_end("tool_call", tool_calls=[{"id": "x", "name": "run_code", "arguments": {}}],
                 input_tokens=4000, output_tokens=100),
        tool_start("x", "run_code"), tool_end("x", "run_code"),
        completed("done"),
    ):
        for output in reduce(energy, event, 12.0):
            printer.print(output, 12.0)
    for output in reduce(review, failed("model overloaded"), 6.0):
        printer.print(output, 6.0)
    # The RunSubAgents call itself prints no tool line.
    for output in reduce(parent, tool_end("t1", "RunSubAgents"), 13.0):
        printer.print(output, 13.0)

    assert buffer.getvalue().splitlines() == [
        "  ● Asking 2 agents: energy, review",
        "    ✓ energy · 1 tool · 12s · 4.1k",
        "    ✗ review ↺ · 0 tools · 6.0s failed: model overloaded",
    ]


def test_detail_level_shows_each_childs_brief_and_tools() -> None:
    out, buffer = renderer(100)
    printer = OutputPrinter(out, detail=lambda: True)
    parent, energy, _review = _companion()
    printer.print(reduce.__globals__["Children"](parent, tuple(parent.children)), 0.0)
    for event in (tool_start("x", "run_code", {"language": "shell", "code": "ls"}),
                  tool_end("x", "run_code", content="a.csv")):
        for output in reduce(energy, event, 2.0):
            printer.print(output, 2.0)
    lines = buffer.getvalue().splitlines()
    assert lines[0] == "  ● Asking 2 agents: energy, review"
    assert lines[1] == "    energy March usage by hour"
    assert lines[2] == "    review ↺ Bills in March"
    assert lines[3] == "  energy │ ✓ $ ls"
    assert lines[4] == "  energy │   a.csv"


def test_a_childs_answer_is_not_printed() -> None:
    parent, energy, _ = _companion()
    text = printed(delta("Child answer.\n\n"), chat_end("response"), view=energy)
    assert text == ""
    del parent


def test_summary_prints_only_at_the_detail_level_and_counts_children() -> None:
    parent, energy, _ = _companion()
    reduce(parent, chat_end("response", input_tokens=100, output_tokens=10), 1.0)
    reduce(energy, chat_end("response", input_tokens=1000, output_tokens=40), 1.0)
    reduce(energy, tool_start("x", "run_code"), 1.0)
    reduce(energy, tool_end("x", "run_code"), 2.0)
    reduce(parent, tool_end("t1", "RunSubAgents"), 3.0)
    reduce(parent, completed("ok"), 8.2)

    out, buffer = renderer(100)
    OutputPrinter(out).summary(parent, 8.2)
    assert buffer.getvalue() == ""
    OutputPrinter(out, detail=lambda: True).summary(parent, 8.2)
    assert buffer.getvalue().strip() == "1 step · 2 tools · 1.1k tokens · 8.2s"


def test_live_rows_are_empty_when_nothing_runs() -> None:
    assert live_rows(None, 0.0, 80) == []
    done = RequestView(agent_id="primary", request_id="r-1", started_at=0.0)
    reduce(done, completed("x"), 1.0)
    assert live_rows(done, 2.0, 80) == []


def test_the_live_row_shows_what_the_request_is_doing() -> None:
    view = RequestView(agent_id="primary", request_id="r-1", started_at=0.0)
    assert "starting" in text_of(live_rows(view, 3.0, 80))
    reduce(view, chat_end("tool_call", tool_calls=[{"id": "t", "name": "s", "arguments": {}}],
                          input_tokens=4000, output_tokens=100), 1.0)
    reduce(view, tool_start("t1", "search", {"query": "pge"}), 1.0)
    row = text_of(live_rows(view, 12.0, 80))
    assert "search(query=pge)" in row
    assert row.rstrip().endswith("12s · 4.1k")
    reduce(view, tool_end("t1", "search"), 13.0)
    assert "thinking" in text_of(live_rows(view, 13.0, 80))
    reduce(view, delta("Partial answer so far"), 14.0)
    rows = text_of(live_rows(view, 14.0, 80))
    # The streamed text itself is drawn by the REPL, above the rows.
    assert "answering" in rows and "Partial answer so far" not in rows
    reduce(view, interaction("i-1", "approval", "Approve?"), 15.0)
    assert "waiting on you" in text_of(live_rows(view, 15.0, 80))


def test_live_rows_show_every_child_at_once() -> None:
    parent, energy, review = _companion()
    reduce(energy, tool_start("x", "run_code", {"language": "python", "code": "load()"}), 1.0)
    reduce(review, completed("ok"), 6.0)
    lines = text_of(live_rows(parent, 12.0, 80)).splitlines()
    assert "waiting on 1 agent" in lines[0]
    assert lines[1].startswith("  ├ energy")
    assert "python: load()" in lines[1]
    # A finished child stays listed, with its mark, while the call runs.
    assert lines[2].startswith("  └ review ↺ ✓ 0 tools")
    assert all(len(line) <= 80 for line in lines)


def test_live_rows_fit_a_narrow_terminal() -> None:
    parent, energy, _ = _companion()
    reduce(energy, tool_start("x", "search", {"query": "a very long query " * 10}), 1.0)
    lines = text_of(live_rows(parent, 2.0, 40)).splitlines()
    assert all(len(line) <= 40 for line in lines)


def test_queued_messages_are_listed_under_the_request() -> None:
    view = RequestView(agent_id="primary", request_id="r-1", started_at=0.0)
    rows = text_of(live_rows(view, 1.0, 80, queued=["and then this", "also that"]))
    assert rows.splitlines()[1:] == ["  queued: and then this", "  queued: also that"]


def test_the_live_row_uses_the_apps_tool_line() -> None:
    view = RequestView(agent_id="primary", request_id="r-1", started_at=0.0)
    reduce(view, tool_start("t1", "search", {"query": "pge"}), 1.0)
    rows = text_of(live_rows(view, 2.0, 80, tool_lines={"search": lambda a: "Looking up bills"}))
    assert "Looking up bills" in rows


def test_the_status_bar_keeps_the_parts_that_fit() -> None:
    assert text_of(status_bar(["fudge", "", "1 bg", "/help"], 80)) == " fudge · 1 bg · /help"
    assert text_of(status_bar(["fudge", "ctrl-c to cancel", "ctrl-z to background"], 30)) == (
        " fudge · ctrl-c to cancel"
    )


def test_a_theme_overrides_the_palette_in_both_libraries() -> None:
    rules = prompt_style_rules({"accent": "magenta"})
    assert rules["prompt"] == "bold magenta"
    assert rules["live.spinner"] == "magenta"
    assert rules["live.muted"] == "#888888"
    assert rich_theme({"accent": "magenta"}).styles["accent"].color.name == "magenta"
