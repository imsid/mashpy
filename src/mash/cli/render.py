"""Terminal output for the CLI.

Two libraries draw, each in its own place. ``rich`` formats finished output
(answers, tool lines, tables), which scrolls up as ordinary terminal text.
``prompt_toolkit`` draws everything below the last finished line: the live
rows of running requests, the input, and the status bar. The functions here
that return formatted text (``live_rows``, ``status_bar``) feed
``prompt_toolkit``; the rest print through a ``rich`` console.
"""

from __future__ import annotations

import asyncio
import io
import re
import sys
from collections.abc import Callable, Mapping
from typing import Any, List, Optional, Protocol

from prompt_toolkit.application import get_app_or_none
from prompt_toolkit.application.run_in_terminal import in_terminal
from rich import box
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import CodeBlock, Heading, Markdown
from rich.markup import escape
from rich.segment import Segment
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text as RichText
from rich.theme import Theme

from mash.runtime.events.trace import tool_call_preview

from .views import (
    CANCELLED,
    COMPLETED,
    FAILED,
    RUN_SUBAGENTS,
    WAITING,
    Children,
    End,
    Output,
    RequestView,
    Step,
    Text,
    Tool,
    ToolCall,
)

# One palette for both libraries: rich styles, and prompt_toolkit classes for
# the live rows and status bar.
DEFAULT_THEME: dict[str, str] = {
    "info": "bold cyan",
    "warn": "bold yellow",
    "error": "bold red",
    "muted": "dim",
    "accent": "cyan",
    "ok": "green",
    "agent": "magenta",
}

# A tool's display line: (arguments) -> one line of text.
ToolLine = Callable[[Mapping[str, Any]], str]

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
OK_MARK = "✓"
ERROR_MARK = "✗"
CANCEL_MARK = "–"
CONTINUED_MARK = "↺"


# Rich's Markdown defaults put inline code on a black background, color
# headings and quotes magenta, and draw tables in cyan. Answers read better in
# the terminal's own colors with a few accents.
MARKDOWN_THEME: dict[str, str] = {
    "markdown.code": "cyan",
    "markdown.code_block": "none",
    "markdown.block_quote": "italic dim",
    "markdown.list": "none",
    "markdown.item.bullet": "dim",
    "markdown.item.number": "dim",
    "markdown.hr": "dim",
    "markdown.h1": "bold underline",
    "markdown.h1.border": "dim",
    "markdown.h2": "bold",
    "markdown.h3": "bold",
    "markdown.h4": "bold italic",
    "markdown.h5": "italic",
    "markdown.h6": "italic dim",
    "markdown.link": "cyan",
    "markdown.link_url": "dim",
    "markdown.table.border": "dim",
    "markdown.table.header": "bold",
}


def rich_theme(theme: Mapping[str, str] | None = None) -> Theme:
    return Theme({**MARKDOWN_THEME, **DEFAULT_THEME, **dict(theme or {})})


# Terminals that support it hold the screen while these bracket a redraw, so
# erasing the live area, printing above it and drawing it again shows as one
# frame. Others ignore them.
# An OSC sequence, such as a terminal hyperlink's start or end.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

SYNC_BEGIN = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"


class TerminalFile(io.TextIOBase):
    """The console's file: text goes above the live area, at once.

    ``patch_stdout`` batches writes on a thread and prints them up to 200ms
    later, while the live area redraws right away: a finished paragraph left
    the live area before it showed up above, and the screen jumped. Here the
    writes made before the next redraw are printed together: the live area is
    erased, the text printed and the live area drawn again, in one frame.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending: list[str] = []
        self._scheduled = False

    def isatty(self) -> bool:
        stream = sys.__stdout__
        return bool(stream is not None and stream.isatty())

    def fileno(self) -> int:
        stream = sys.__stdout__
        if stream is None:
            raise OSError("no terminal")
        return stream.fileno()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(sys.__stdout__, "encoding", None) or "utf-8"

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not text:
            return 0
        app = get_app_or_none()
        loop = getattr(app, "loop", None)
        if app is None or not app.is_running or loop is None or app.is_done:
            sys.stdout.write(text)
            sys.stdout.flush()
            return len(text)
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            self._queue(app, text)
        else:
            loop.call_soon_threadsafe(self._queue, app, text)
        return len(text)

    def flush(self) -> None:
        return None

    def _queue(self, app: Any, text: str) -> None:
        self._pending.append(text)
        if not self._scheduled:
            # Created now, so it runs before the redraw this write's caller
            # asks for next.
            self._scheduled = True
            app.loop.create_task(self._emit(app))

    def _take(self) -> str:
        text = "".join(self._pending)
        self._pending.clear()
        self._scheduled = False
        return text

    async def _emit(self, app: Any) -> None:
        output = app.output
        output.write_raw(SYNC_BEGIN)
        try:
            async with in_terminal():
                # prompt_toolkit turns autowrap off while it draws; long
                # lines printed here (links) wrap like ordinary output.
                output.enable_autowrap()
                output.write_raw(self._take())
                output.flush()
        finally:
            if self._scheduled:
                # Cancelled before printing: print what waited, as is.
                sys.stdout.write(self._take())
                sys.stdout.flush()
            output.write_raw(SYNC_END)
            output.flush()


def prompt_style_rules(theme: Mapping[str, str] | None = None) -> dict[str, str]:
    """The palette as prompt_toolkit style rules for the live area."""
    palette = {**DEFAULT_THEME, **dict(theme or {})}

    def ptk(style: str) -> str:
        # rich says "dim"; prompt_toolkit has no dim, so use a gray.
        return " ".join("#888888" if word == "dim" else word for word in style.split())

    return {
        "prompt": ptk(f"bold {palette['accent']}"),
        "live.spinner": ptk(palette["accent"]),
        "live.agent": ptk(palette["agent"]),
        "live.ok": ptk(palette["ok"]),
        "live.error": ptk(palette["error"]),
        "live.warn": ptk(palette["warn"]),
        "live.muted": ptk(palette["muted"]),
        "ask.checked": ptk(f"bold {palette['accent']}"),
        "ask.number": ptk(palette["muted"]),
        "ask.hint": ptk(palette["muted"]),
        # The bar is plain text on the terminal's own background.
        "bottom-toolbar": "noreverse " + ptk(palette["muted"]),
        "bottom-toolbar.text": "noreverse " + ptk(palette["muted"]),
    }


def fmt_tokens(count: int) -> str:
    """``950``, ``4.1k``, ``18k``, ``1.2M``."""
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}k"
    if count < 1_000_000:
        return f"{count // 1000}k"
    return f"{count / 1_000_000:.1f}M"


def fmt_seconds(seconds: float) -> str:
    """``0.8s``, ``12s``, ``1m05s``."""
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def clip(text: str, width: int) -> str:
    """``text`` on one line, at most ``width`` characters."""
    flat = " ".join(str(text or "").split())
    if width <= 0:
        return ""
    return flat if len(flat) <= width else flat[: max(0, width - 1)] + "…"


def tool_display(call: ToolCall, tool_lines: Mapping[str, ToolLine] | None = None) -> str:
    """One line for a tool call: the app's own line for that tool, or a preview."""
    custom = (tool_lines or {}).get(call.name)
    if custom is not None:
        try:
            line = custom(call.arguments)
        except Exception:
            line = ""
        if line:
            return clip(line, 200)
    return tool_call_preview(call.name, call.arguments)


class _PlainCodeBlock(CodeBlock):
    """A code block with no padding or background, so copied code is clean."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        code = str(self.text).rstrip()
        text = Syntax(
            code, self.lexer_name, theme=self.theme, background_color="default"
        ).highlight(code)
        text.rstrip()
        # A line longer than the width continues on the next line, cut at the
        # width: cropped, the printed code would miss what did not fit, and
        # wrapped at words, it would lose the spaces where it broke.
        width = max(1, options.max_width)
        lines = []
        for line in text.split("\n", allow_blank=True):
            if len(line) > width:
                lines.extend(line.divide(range(width, len(line), width)))
            else:
                lines.append(line)
        folded = RichText("\n").join(lines)
        folded.no_wrap = True
        yield folded


class _LeftHeading(Heading):
    """A heading on the left, in its style: no centered or boxed titles."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        text = self.text.copy()
        text.justify = "left"
        yield text


class AnswerMarkdown(Markdown):
    """Markdown as the CLI prints answers: plain code blocks, terminal colors."""

    elements = {
        **Markdown.elements,
        "fence": _PlainCodeBlock,
        "code_block": _PlainCodeBlock,
        "heading_open": _LeftHeading,
    }

    def __init__(self, markup: str) -> None:
        # ansi_dark leaves plain code in the terminal's own foreground color.
        super().__init__(markup, code_theme="ansi_dark")


def _rstrip_line(line: list[Segment]) -> list[Segment]:
    """A rendered line without the spaces Rich pads it with to the width."""
    segments = list(line)
    while segments:
        last = segments[-1]
        if last.control:
            break
        text = last.text.rstrip(" ")
        if text:
            segments[-1] = Segment(text, last.style)
            break
        segments.pop()
    return segments


class Trimmed:
    """A renderable printed without trailing spaces or blank lines around it.

    Rich pads each line to the console width and ends lists, quotes and
    tables with a blank line. Printed as is, copied text carries the padding
    and blocks printed one by one get uneven gaps; the renderer adds the gaps
    between blocks itself.
    """

    def __init__(self, renderable: Any) -> None:
        self.renderable = renderable

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        lines = [
            _rstrip_line(line)
            for line in console.render_lines(self.renderable, options, pad=False)
        ]
        while lines and not "".join(segment.text for segment in lines[0]).strip():
            lines.pop(0)
        while lines and not "".join(segment.text for segment in lines[-1]).strip():
            lines.pop()
        for line in lines:
            yield from line
            yield Segment.line()


class Renderer(Protocol):
    """Protocol for renderers."""

    def info(self, text: str) -> None:
        """Render informational text."""
        ...

    def link(self, text: str) -> None:
        """Render a line holding a link, never broken across lines."""
        ...

    def section(self, text: str, style: str = "info") -> None:
        """A line that starts a new part of the output, after a blank line."""
        if self._last not in (None, "blank"):
            self._console.print()
        self._console.print(text, style=style)
        self._last = "line"

    def warn(self, text: str) -> None:
        """Render warning text."""
        ...

    def error(self, text: str) -> None:
        """Render error text."""
        ...

    def markdown(self, text: str) -> None:
        """Render Markdown-formatted text."""
        ...

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
        """Render a table."""
        ...

    def clear(self) -> None:
        """Clear the terminal screen."""
        ...


class RichRenderer:
    """Rich-based renderer for formatted CLI output."""

    def __init__(
        self,
        console: Optional[Console] = None,
        *,
        theme: Mapping[str, str] | None = None,
    ) -> None:
        self._console = console or Console(file=TerminalFile(), theme=rich_theme(theme))
        # What was printed last, for the blank lines between blocks: None,
        # "blank", "line" (one-line output), "markdown", or "echo" (the
        # user's message, which everything after it is set apart from).
        self._last: str | None = None

    @property
    def console(self) -> Console:
        """Get the underlying Rich console."""
        return self._console

    def _gap(self, kind: str) -> bool:
        """Whether a blank line goes before output of ``kind``."""
        last = self._last
        if last in (None, "blank"):
            return False
        if last in ("echo", "markdown"):
            return True
        return kind == "markdown"

    def _emit(self, kind: str, *args: Any, **kwargs: Any) -> None:
        if self._gap(kind):
            self._console.print()
        self._console.print(*args, **kwargs)
        self._last = kind

    def info(self, text: str) -> None:
        """Render informational text."""
        self._emit("line", text, style="info")

    def muted(self, text: str) -> None:
        """Render secondary text."""
        self._emit("line", text, style="muted")

    def link(self, text: str) -> None:
        """Render a line holding a link, never broken across lines."""
        # Rich breaks long lines with real newlines, which breaks a URL for
        # anyone copying it; soft wrapping leaves the wrapping to the terminal.
        self._emit("line", text, style="info", soft_wrap=True)

    def section(self, text: str, style: str = "info") -> None:
        """A line that starts a new part of the output, after a blank line."""
        if self._last not in (None, "blank"):
            self._console.print()
        self._console.print(text, style=style)
        self._last = "line"

    def warn(self, text: str) -> None:
        """Render warning text."""
        self._emit("line", text, style="warn")

    def error(self, text: str) -> None:
        """Render error text."""
        self._emit("line", text, style="error")

    def echo(self, text: str, prompt: str, continuation: str) -> None:
        """The user's message as sent, under the prompt mark."""
        lines = text.split("\n")
        rendered = RichText(prompt, style="accent bold")
        rendered.append(lines[0])
        for line in lines[1:]:
            rendered.append("\n" + continuation + line)
        if self._last not in (None, "blank"):
            self._console.print()
        self._console.print(rendered, soft_wrap=True, highlight=False)
        self._last = "echo"

    def markdown(self, text: str, *, dim: bool = False) -> None:
        """Render Markdown-formatted text, with nothing drawn around it."""
        if not text.strip():
            return
        self._emit("markdown", Trimmed(AnswerMarkdown(text)), style="dim" if dim else None)

    def gap_before_markdown(self) -> bool:
        """Whether the next Markdown block prints after a blank line."""
        return self._gap("markdown")

    def render_markdown(self, text: str, width: int) -> str:
        """``text`` as it would print, in ANSI codes, for the live area.

        Terminal hyperlinks are left out: prompt_toolkit cannot draw them, so
        a link shows as its text until the block prints.
        """
        with self._console.capture() as capture:
            self._console.print(Trimmed(AnswerMarkdown(text)), width=max(20, width))
        return _OSC.sub("", capture.get())

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
        """Render a table: the first column accented, the last one wrapping."""
        table = Table(box=box.SIMPLE_HEAD, header_style="bold", show_edge=False)
        for index, header in enumerate(headers):
            last = index == len(headers) - 1 and len(headers) > 1
            table.add_column(
                header,
                style="accent" if index == 0 else None,
                no_wrap=not last,
                overflow="fold" if last else "ellipsis",
            )
        for row in rows:
            table.add_row(*row)
        self._emit("line", Trimmed(table))

    def clear(self) -> None:
        """Clear the terminal screen."""
        self._console.clear()
        self._last = None

    def print(self, *args: Any, **kwargs: Any) -> None:
        """Print to console; with nothing to print, a blank line."""
        if not args:
            if self._last != "blank":
                self._console.print(**kwargs)
            self._last = "blank"
            return
        self._emit("line", *args, **kwargs)


def _mark(view: RequestView) -> tuple[str, str]:
    """(mark, style name) for how a request ended."""
    if view.status == COMPLETED:
        return OK_MARK, "ok"
    if view.status == FAILED:
        return ERROR_MARK, "error"
    return CANCEL_MARK, "muted"


def _error_line(text: str) -> str:
    """The line of an error that says what went wrong.

    A Python traceback says it last; most other errors say it first.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    if lines[0].startswith("Traceback"):
        return lines[-1]
    return lines[0]


def _stats(view: RequestView, now: float) -> str:
    parts = [fmt_seconds(view.elapsed(now))]
    if view.tokens:
        parts.append(fmt_tokens(view.tokens))
    return " · ".join(parts)


class OutputPrinter:
    """Prints each ``Output`` once, as finished lines above the input.

    ``detail`` is read on every call, so the level can change mid-request.
    At the default level a request shows its answer and one line per tool
    call. The detail level adds each model turn's tokens, time and thinking,
    tool results, and the tool calls of child requests.
    """

    def __init__(
        self,
        renderer: RichRenderer,
        *,
        detail: Callable[[], bool] = lambda: False,
        tool_lines: Mapping[str, ToolLine] | None = None,
    ) -> None:
        self._renderer = renderer
        self._detail = detail
        self._tool_lines = tool_lines

    def _print(self, line: str) -> None:
        self._renderer.print(line, soft_wrap=True, highlight=False)

    @staticmethod
    def _prefix(view: RequestView) -> str:
        """Indent, and for a child the agent it belongs to."""
        if view.parent is None:
            return "  "
        return f"  [agent]{escape(view.agent_id)}[/agent] [muted]│[/muted] "

    def print(self, output: Output, now: float) -> None:
        if isinstance(output, Text):
            # A child's answer goes to the companion, which writes the reply.
            if output.view.parent is None:
                self._renderer.markdown(output.markdown)
        elif isinstance(output, Step):
            self._step(output)
        elif isinstance(output, Tool):
            self._tool(output)
        elif isinstance(output, Children):
            self._children(output)
        elif isinstance(output, End) and output.view.parent is not None:
            self.child_end(output.view, now)

    def _step(self, step: Step) -> None:
        view = step.view
        if self._detail():
            tokens = f"{fmt_tokens(step.input_tokens)} in / {fmt_tokens(step.output_tokens)} out"
            if step.reasoning_tokens:
                tokens += f" / {fmt_tokens(step.reasoning_tokens)} reasoning"
            what = ", ".join(step.tool_names) if step.tool_names else step.action
            self._print(
                f"{self._prefix(view)}[muted]step {step.number} · {escape(what)} · "
                f"{tokens} · {fmt_seconds(step.duration_ms / 1000)}[/muted]"
            )
            if step.thinking:
                grid = Table.grid(padding=(0, 0))
                grid.add_column(no_wrap=True)
                grid.add_column()
                grid.add_row("    ", RichText(step.thinking, style="dim italic"))
                self._renderer.print(grid)
        if step.preamble and view.parent is None:
            self._renderer.markdown(step.preamble)

    def _tool(self, tool: Tool) -> None:
        view, call = tool.view, tool.call
        # A RunSubAgents call is shown by its children's lines.
        if call.name == RUN_SUBAGENTS and view.children:
            return
        if view.parent is not None and not self._detail():
            return
        line = escape(tool_display(call, self._tool_lines))
        seconds = (call.duration_ms or 0) / 1000
        timing = f" [muted]{fmt_seconds(seconds)}[/muted]" if seconds >= 1 else ""
        prefix = self._prefix(view)
        if call.is_error:
            reason = escape(clip(_error_line(call.result), 160))
            self._print(f"{prefix}[error]{ERROR_MARK}[/error] {line}{timing}")
            if reason:
                self._print(f"{prefix}  [muted]{reason}[/muted]")
            return
        self._print(f"{prefix}[ok]{OK_MARK}[/ok] {line}{timing}")
        if self._detail() and call.result.strip():
            self._print(f"{prefix}  [muted]{escape(clip(call.result, 200))}[/muted]")

    def _children(self, started: Children) -> None:
        names = ", ".join(escape(child.agent_id) for child in started.children)
        count = len(started.children)
        noun = "agent" if count == 1 else "agents"
        self._print(f"  [accent]●[/accent] Asking {count} {noun}: {names}")
        if self._detail():
            for child in started.children:
                mark = f" {CONTINUED_MARK}" if child.continued else ""
                self._print(
                    f"    [agent]{escape(child.agent_id)}[/agent]{mark} "
                    f"[muted]{escape(clip(child.brief, 160))}[/muted]"
                )

    def child_end(self, view: RequestView, now: float) -> None:
        """One line for a child that ended."""
        mark, style = _mark(view)
        continued = f" {CONTINUED_MARK}" if view.continued else ""
        tools = f"{view.tool_count} tool" + ("" if view.tool_count == 1 else "s")
        line = (
            f"    [{style}]{mark}[/{style}] [agent]{escape(view.agent_id)}[/agent]"
            f"{continued} [muted]· {tools} · {_stats(view, now)}[/muted]"
        )
        if view.status == FAILED:
            line += f" [muted]·[/muted] [error]failed: {escape(clip(view.error or '', 160))}[/error]"
        elif view.status == CANCELLED:
            line += " [muted]· cancelled[/muted]"
        self._print(line)

    def summary(self, view: RequestView, now: float) -> None:
        """The detail level's closing line: steps, tools, tokens and time."""
        if not self._detail():
            return
        steps = f"{view.steps} step" + ("" if view.steps == 1 else "s")
        tool_total = view.total_tools()
        tools = f"{tool_total} tool" + ("" if tool_total == 1 else "s")
        self._print(
            f"  [muted]{steps} · {tools} · {fmt_tokens(view.total_tokens())} tokens · "
            f"{fmt_seconds(view.elapsed(now))}[/muted]"
        )


# Formatted text for prompt_toolkit: (style, text) pairs.
Fragments = list[tuple[str, str]]


def _activity(
    view: RequestView, tool_lines: Mapping[str, ToolLine] | None
) -> str:
    """What a running request is doing, in a few words."""
    if view.status == WAITING:
        return "waiting on you"
    tool = view.current_tool()
    if tool is not None:
        if tool.name == RUN_SUBAGENTS and view.children:
            running = sum(1 for child in view.children if not child.ended)
            return f"waiting on {running} agent" + ("" if running == 1 else "s")
        display = tool_display(tool, tool_lines)
        return f"{display} · {tool.note}" if tool.note else display
    if view.phase in ("answering", "starting", "finishing"):
        return view.phase
    return "thinking"


def _row(
    view: RequestView,
    now: float,
    width: int,
    tool_lines: Mapping[str, ToolLine] | None,
    *,
    lead: str,
    name_width: int,
) -> Fragments:
    """One live row: lead, name, spinner or mark, activity, time and tokens."""
    stats = _stats(view, now)
    fragments: Fragments = [("class:live.muted", lead)]
    used = len(lead)
    if view.parent is not None:
        name = view.agent_id.ljust(name_width)
        mark = f" {CONTINUED_MARK}" if view.continued else ""
        fragments.append(("class:live.agent", name))
        fragments.append(("class:live.muted", mark + " "))
        used += len(name) + len(mark) + 1
    if view.ended:
        mark, style = _mark(view)
        fragments.append((f"class:live.{style}", mark + " "))
        activity = f"{view.tool_count} tool" + ("" if view.tool_count == 1 else "s")
        if view.status == FAILED:
            activity = f"failed: {view.error or ''}"
        elif view.status == CANCELLED:
            activity = "cancelled"
    else:
        style = "live.warn" if view.status == WAITING else "live.spinner"
        fragments.append((f"class:{style}", SPINNER[int(now * 10) % len(SPINNER)] + " "))
        activity = _activity(view, tool_lines)
    used += 2
    room = max(8, width - used - len(stats) - 3)
    fragments.append(("", clip(activity, room)))
    fragments.append(("class:live.muted", f"  {stats}"))
    fragments.append(("", "\n"))
    return fragments


def live_rows(
    view: RequestView | None,
    now: float,
    width: int,
    *,
    queued: list[str] | None = None,
    tool_lines: Mapping[str, ToolLine] | None = None,
) -> Fragments:
    """The live area above the input: the running request and its children.

    Empty when nothing runs and nothing is queued. Rows are clipped to
    ``width`` so each stays on one line and redraws in place.
    """
    fragments: Fragments = []
    if view is not None and not view.ended:
        fragments.extend(_row(view, now, width, tool_lines, lead="", name_width=0))
        children = [child for child in view.children if _show_child(view, child)]
        name_width = max((len(child.agent_id) for child in children), default=0)
        for index, child in enumerate(children):
            lead = "  └ " if index == len(children) - 1 else "  ├ "
            fragments.extend(
                _row(child, now, width, tool_lines, lead=lead, name_width=name_width)
            )
    for message in queued or []:
        fragments.append(("class:live.muted", f"  queued: {clip(message, width - 12)}\n"))
    return fragments


def _show_child(parent: RequestView, child: RequestView) -> bool:
    """A child stays in the live area while its RunSubAgents call runs."""
    tool = parent.current_tool()
    return (tool is not None and tool.name == RUN_SUBAGENTS) or not child.ended


def status_bar(parts: list[str], width: int) -> Fragments:
    """The bar under the input: the parts that fit, joined with dots."""
    kept: list[str] = []
    for part in parts:
        if not part:
            continue
        if len(" · ".join(kept + [part])) > width - 2:
            break
        kept.append(part)
    return [("class:bottom-toolbar.text", " " + " · ".join(kept))]
