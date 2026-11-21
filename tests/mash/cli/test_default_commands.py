"""The built-in slash commands, run in a REPL on a pipe against a scripted host."""

from __future__ import annotations

import asyncio
from typing import Any

from mash.cli.client import MashHostClientError
from tests.mash.cli.repl_harness import (
    ENTER,
    FakeHost,
    Gate,
    ReplDriver,
    chat_end,
    completed,
    delta,
    run_repl,
    trace,
)


def run(script, host: FakeHost, **kwargs: Any) -> ReplDriver:
    return asyncio.run(run_repl(script, host, **kwargs))


def say(*lines: str, host: FakeHost | None = None, **kwargs: Any) -> tuple[str, FakeHost]:
    """Run slash commands one after another; return the output and the host."""
    host = host or FakeHost()

    async def script(d: ReplDriver) -> None:
        for line in lines:
            await d.say(line)

    return run(script, host, **kwargs).output, host


def _lines(output: str) -> list[str]:
    return [line.rstrip() for line in output.splitlines()]


AGENTS = [
    {"agent_id": "energy", "metadata": {
        "display_name": "Energy Agent", "description": "Reads usage and bills.",
        "capabilities": ["usage", "bills"], "usage_guidance": "Questions about energy use."}},
    {"agent_id": "review", "metadata": None},
]


# --- /help and /keys ---------------------------------------------------------


def test_help_groups_the_commands() -> None:
    lines = _lines(say("/help")[0])
    headings = [line for line in lines if line in ("Session", "Requests", "Deployment", "REPL")]
    assert headings == ["Session", "Requests", "Deployment", "REPL"]
    session = lines[lines.index("Session") + 1 : lines.index("Requests")]
    assert [line.split()[0] for line in session if line] == [
        "/compact", "/copy", "/new", "/resume", "/usage"
    ]
    assert any(line.startswith("  /exit") and "(also /quit, /q)" in line for line in lines)
    for removed in ("/status", "/session ", "/sessions", "/history", "/host", "/agent "):
        assert not any(line.strip().startswith(removed) for line in lines)


def test_an_apps_commands_are_listed_under_their_own_group_or_more() -> None:
    from mash.cli.commands import Command

    host = FakeHost()

    async def script(d: ReplDriver) -> None:
        d.repl.register_command(Command(name="bills", help="Show bills", handler=lambda c, a: None,
                                        group="Fudge"))
        d.repl.register_command(Command(name="zap", help="Zap", handler=lambda c, a: None))
        await d.say("/help")

    lines = _lines(run(script, host).output)
    order = [line for line in lines if line in ("Deployment", "Fudge", "More", "REPL")]
    assert order == ["Deployment", "Fudge", "More", "REPL"]
    assert lines[lines.index("Fudge") + 1].split()[:1] == ["/bills"]


def test_keys_lists_the_keys() -> None:
    out, _ = say("/keys")
    assert "Ctrl-O" in out and "Show or hide model steps" in out
    assert "Ctrl-Z" not in out
    assert "\\ then Enter" in out


# --- /agents and /tools ------------------------------------------------------


def test_agents_describes_the_hosts_agents() -> None:
    host = FakeHost()
    host.agents = AGENTS
    lines = _lines(say("/agents", host=host, host_id="fudge")[0])
    start = lines.index("energy · Energy Agent")
    assert lines[start + 1 : start + 4] == [
        "  Reads usage and bills.",
        "  Capabilities: usage, bills",
        "  Use for: Questions about energy use.",
    ]
    # An agent registered without metadata is still listed.
    assert "review" in lines


def test_agents_for_a_bare_agent_describes_that_agent() -> None:
    host = FakeHost()
    host.agents = [{"agent_id": "primary", "metadata": {
        "display_name": "Primary", "description": "Does everything.",
        "capabilities": [], "usage_guidance": ""}}]
    lines = _lines(say("/agents", host=host)[0])
    assert "primary · Primary" in lines
    assert "  Does everything." in lines
    assert not any(line.startswith("  Capabilities") for line in lines)


def test_tools_lists_the_tools_and_skills_of_the_targets_agents() -> None:
    host = FakeHost()
    host.agents = AGENTS
    host.tools = [
        {"tool": {"name": "run_code", "description": "Runs code in the sandbox."},
         "agents": ["energy", "other"]},
        {"tool": {"name": "secret", "description": "Not for this host."}, "agents": ["other"]},
    ]
    host.skills = [{"skill": {"name": "billing", "description": "How bills work."},
                    "agents": ["review"]}]
    out, _ = say("/tools", host=host, host_id="fudge")
    assert "run_code" in out and "Runs code in the sandbox." in out
    assert "billing" in out and "How bills work." in out
    assert "secret" not in out
    # The agent outside the host is not named.
    assert "other" not in out


def test_tools_shows_the_first_sentence_of_each_description_in_full() -> None:
    host = FakeHost()
    host.agents = AGENTS
    first = (
        "Run code on your computer: a Linux sandbox that belongs to the user you "
        "are working for, where files persist between calls."
    )
    host.tools = [
        {"tool": {"name": "run_code", "description": first + " Use it to analyse data.\n\nMore."},
         "agents": ["energy"]},
    ]
    out, _ = say("/tools", host=host, host_id="fudge")
    flat = " ".join(out.split())
    assert first in flat
    assert "Use it to analyse data" not in flat


def test_tools_says_when_there_are_none() -> None:
    assert "These agents have no tools or skills." in say("/tools")[0]


# --- /usage --------------------------------------------------------------------


def test_usage_shows_each_agent_and_the_subagents_it_ran() -> None:
    host = FakeHost()
    host.session = {
        "usage": {
            "request_count": 2,
            "subagent_count": 3,
            "total_tokens": 61_800,
            "input_tokens": 58_000,
            "output_tokens": 3800,
            "cache_read_tokens": 30_000,
            "cache_write_tokens": 900,
            "agents": [
                {"agent_id": "companion", "models": ["claude-haiku-4-5"],
                 "request_count": 2, "subagent_count": 0, "input_tokens": 8000,
                 "output_tokens": 800, "cache_read_tokens": 6000, "cache_write_tokens": 0},
                {"agent_id": "research", "models": ["claude-sonnet-4-6"],
                 "request_count": 0, "subagent_count": 3, "input_tokens": 50_000,
                 "output_tokens": 3000, "cache_read_tokens": 24_000,
                 "cache_write_tokens": 900},
            ],
        }
    }
    out, _ = say("/usage", host=host)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    assert "2 requests · 3 subagent runs · 62k tokens" in lines
    assert any(
        line.split() == ["companion", "claude-haiku-4-5", "2", "8.0k", "800", "6.0k", "0"]
        for line in lines
    )
    assert any(
        line.split() == ["↳", "research", "claude-sonnet-4-6", "3", "50k", "3.0k", "24k", "900"]
        for line in lines
    )
    assert any(
        line.split() == ["Total", "5", "58k", "3.8k", "30k", "900"] for line in lines
    )
    assert "Today" not in out


def test_usage_of_one_agent_has_no_total_row() -> None:
    host = FakeHost()
    host.session = {
        "usage": {
            "request_count": 1, "subagent_count": 0, "total_tokens": 1200,
            "agents": [
                {"agent_id": "primary", "models": ["gpt-5"], "request_count": 1,
                 "subagent_count": 0, "input_tokens": 1000, "output_tokens": 200,
                 "cache_read_tokens": 0, "cache_write_tokens": 0},
            ],
        }
    }
    out, _ = say("/usage", host=host)
    assert "1 request · 1.2k tokens" in out
    assert "subagent" not in out
    assert "Total" not in out


# --- /memory -------------------------------------------------------------------


def test_memory_without_cortex_says_so() -> None:
    for line in ("/memory", "/memory add likes tea", "/memory forget e1"):
        assert "Cortex is not enabled on this deployment." in say(line)[0]


def test_memory_shows_the_profile_and_entries() -> None:
    host = FakeHost()
    host.cortex = {"profile": "Lives in **Oakland**.", "entries": [
        {"entry_id": "abc12345-0000", "topic": "home", "content": "Has solar panels.",
         "origin": "build"},
        {"entry_id": "def67890-0000", "topic": "note", "content": "Prefers email.",
         "origin": "app"},
    ]}
    out, _ = say("/memory", host=host)
    assert "Lives in Oakland." in out
    assert "abc12345" in out and "Has solar panels." in out and "conversations" in out
    row = next(line for line in out.splitlines() if "def67890" in line)
    assert "Prefers email." in row and row.rstrip().endswith("the app")


def test_memory_with_nothing_remembered_says_so() -> None:
    host = FakeHost()
    host.cortex = {"profile": "", "entries": []}
    assert "Nothing is remembered in this app yet." in say("/memory", host=host)[0]


def test_memory_add_pins_a_fact_and_forget_removes_it() -> None:
    host = FakeHost()
    host.cortex = {"profile": "", "entries": []}
    out, host = say(
        "/memory add likes short answers",
        "/memory add diet: vegetarian",
        "/memory forget e1",
        "/memory forget zz",
        "/memory add",
        host=host,
    )
    assert "Remembered (e1-0000-)." in out and "Remembered (e2-0000-)." in out
    assert "Forgot: likes short answers" in out
    assert "No entry matches 'zz'." in out
    assert "Usage: /memory | /memory add <text> | /memory forget <id>" in out
    # A "topic: fact" names the topic; anything else is a note.
    assert host.cortex["entries"] == [
        {"entry_id": "e2-0000-aaaa", "topic": "diet", "content": "vegetarian", "origin": "app"}
    ]


def test_memory_forget_with_an_ambiguous_id_removes_nothing() -> None:
    host = FakeHost()
    host.cortex = {"profile": "", "entries": [
        {"entry_id": "e1-a", "topic": "t", "content": "one", "origin": "app"},
        {"entry_id": "e1-b", "topic": "t", "content": "two", "origin": "app"},
    ]}
    out, host = say("/memory forget e1", host=host)
    assert "'e1' matches 2 entries; type more of the id." in out
    assert len(host.cortex["entries"]) == 2


# --- /browser ------------------------------------------------------------------


def test_browser_lists_sites_and_signs_out_of_one() -> None:
    host = FakeHost()
    host.browser_sites = [{"domain": "pge.com", "cookies": 12}, {"domain": "x.org", "cookies": 1}]
    out, host = say("/browser", "/browser forget x.org", "/browser nope", host=host)
    assert "pge.com" in out and "12" in out
    assert "Signed out of x.org (1 cookie removed)." in out
    assert "Usage: /browser | /browser forget <site>" in out
    assert host.browser_sites == [{"domain": "pge.com", "cookies": 12}]


def test_browser_with_no_sites_and_with_no_browsers() -> None:
    host = FakeHost()
    host.browser_sites = []
    assert "The policy's browser is not signed in to any site." in say("/browser", host=host)[0]
    assert "Command failed: this host has no browsers" in say("/browser")[0]


# --- /copy ---------------------------------------------------------------------


ANSWER = "Run this:\n\n```python\nprint('a')\n```\n\nthen this:\n\n```sh\nls -l\n```\n"


def test_copy_puts_the_last_answer_or_its_last_code_block_on_the_clipboard() -> None:
    host = FakeHost()
    host.script("r-1", chat_end("response", text=ANSWER), completed(ANSWER))
    copied: list[str] = []

    async def script(d: ReplDriver) -> None:
        await d.say("/copy")
        await d.say("how")
        await d.say("/copy")
        copied.append(d.clipboard.get_data().text)
        await d.say("/copy code")
        copied.append(d.clipboard.get_data().text)
        await d.say("/copy everything")

    out = run(script, host).output
    assert copied == [ANSWER, "ls -l"]
    assert "No answer to copy yet." in out
    assert f"Copied the last answer ({len(ANSWER)} characters)." in out
    assert "Copied the last code block (5 characters)." in out
    assert "Usage: /copy | /copy code" in out


def test_copy_takes_a_streamed_answer_and_says_when_there_is_no_code() -> None:
    host = FakeHost()
    host.script("r-1", trace("chat.start"), delta("Just words."),
                chat_end("response", text="Just words."), completed("Just words."))
    copied: list[str] = []

    async def script(d: ReplDriver) -> None:
        await d.say("hi")
        await d.say("/copy code")
        await d.say("/copy")
        copied.append(d.clipboard.get_data().text)

    out = run(script, host).output
    assert copied == ["Just words."]
    assert "The last answer has no code block." in out


def test_copy_without_a_system_clipboard_says_where_the_text_went() -> None:
    from mash.cli.input import SystemClipboard

    host = FakeHost()
    host.script("r-1", chat_end("response", text="Words."), completed("Words."))

    async def script(d: ReplDriver) -> None:
        clipboard = SystemClipboard()
        clipboard._system = None
        d.repl._clipboard = clipboard
        await d.say("hi")
        await d.say("/copy")
        assert clipboard.get_data().text == "Words."

    out = run(script, host).output
    assert "No system clipboard here. Ctrl-Y pastes the last answer into the input." in out


# --- /retry and /compact --------------------------------------------------------


def test_retry_runs_the_last_request_again_and_follows_it() -> None:
    host = FakeHost()
    host.script("r-1", chat_end("response", text="First try."), completed("First try."))
    host.script("r-2", chat_end("response", text="Second try."), completed("Second try."))

    async def script(d: ReplDriver) -> None:
        await d.say("/retry")
        await d.say("go")
        d.type("/retry" + ENTER)
        await d.until(lambda: "Second try." in d.output)
        await d.idle()

    d = run(script, host)
    assert "Nothing to retry yet." in d.output
    # The last message again, as a new event.
    assert [s["message"] for s in host.submitted] == ["go", "go"]
    assert len(set(host.events)) == 2
    assert d.repl.context.last_request_id == "r-2"
    assert d.repl.last_answer == "Second try."


def test_retry_and_compact_wait_for_the_running_request() -> None:
    host = FakeHost()
    gate = Gate()
    host.script("r-1", trace("chat.start"), gate, chat_end("response", text="ok"), completed("ok"))

    async def script(d: ReplDriver) -> None:
        d.type("go" + ENTER)
        await d.until(d.repl.running)
        await d.command("/retry")
        await d.command("/compact")
        gate.open()
        await d.idle()

    out = run(script, host).output
    assert out.count("A request is running. Wait for it, or press Ctrl-C to cancel it.") == 2
    assert len(host.submitted) == 1
    assert not any(call[0] == "compact_session" for call in host.calls)


def test_compact_summarizes_the_session() -> None:
    host = FakeHost()
    host.summary = "The user asked about March bills."
    host.session = {"total_tokens": 900}
    out, host = say("/compact", host=host)
    assert "Session summarized. Later requests start from:" in out
    assert "The user asked about March bills." in out
    assert ("compact_session", ("companion", "s-1")) in host.calls


def test_compact_with_nothing_to_summarize_says_so() -> None:
    assert "Nothing to summarize yet." in say("/compact")[0]


def test_a_command_the_host_refuses_is_reported() -> None:
    host = FakeHost()

    def refuse(agent_id: str, session_id: str) -> dict[str, Any]:
        raise MashHostClientError("agent is disabled", status_code=409)

    host.compact_session = refuse  # type: ignore[method-assign]
    assert "Command failed: agent is disabled" in say("/compact", host=host)[0]


# --- argument menus ---------------------------------------------------------------


def test_the_argument_menu_offers_subcommands_and_subagent_numbers() -> None:
    from mash.cli.views import RequestView

    host = FakeHost()

    async def script(d: ReplDriver) -> None:
        repl = d.repl
        assert [word for word, _ in repl._argument_menu("workflow", [])] == [
            "list", "run", "status", "resume",
        ]
        assert [word for word, _ in repl._argument_menu("copy", [])] == ["code"]
        assert repl._argument_menu("nope", []) == []
        assert repl._argument_menu("subagent", []) == []
        parent = RequestView(agent_id="companion", request_id="r", started_at=0.0)
        done = RequestView(agent_id="energy", request_id="c-0", started_at=0.0,
                           brief="usage", parent=parent, status="completed", ended_at=1.0)
        going = RequestView(agent_id="review", request_id="c-1", started_at=0.0,
                            brief="bills", parent=parent)
        parent.children.extend([done, going])
        repl.last_parent = parent
        assert repl._argument_menu("subagent", []) == [
            ("1", "energy: usage"), ("2", "review: bills"),
        ]
        assert repl._argument_menu("bg", []) == []

    run(script, host)
