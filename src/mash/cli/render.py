"""Terminal output for the CLI.

Two libraries draw, each in its own place. ``rich`` formats finished output
(answers, tool lines, tables), which scrolls up as ordinary terminal text.
``prompt_toolkit`` draws everything below the last finished line: the live
rows of running requests, the input, and the status bar. The functions here
that return formatted text (``live_rows``, ``status_bar``) feed
``prompt_toolkit``; the rest print through a ``rich`` console.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, List, Optional, Protocol

from rich import box
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import CodeBlock, Markdown
from rich.markup import escape
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


def rich_theme(theme: Mapping[str, str] | None = None) -> Theme:
    return Theme({**DEFAULT_THEME, **dict(theme or {})})


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
        yield text


class AnswerMarkdown(Markdown):
    """Markdown as the CLI prints answers: plain code blocks, terminal colors."""

    elements = {
        **Markdown.elements,
        "fence": _PlainCodeBlock,
        "code_block": _PlainCodeBlock,
    }

    def __init__(self, markup: str) -> None:
        # ansi_dark leaves plain code in the terminal's own foreground color.
        super().__init__(markup, code_theme="ansi_dark")


class Renderer(Protocol):
    """Protocol for renderers."""

    def info(self, text: str) -> None:
        """Render informational text."""
        ...

    def link(self, text: str) -> None:
        """Render a line holding a link, never broken across lines."""
        ...

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
        # Rich tables should own wrapping/layout. Enabling console-level soft
        # wrapping lets the terminal wrap rendered box lines after layout,
        # which can visually clip bordered output at narrow widths.
        self._console = console or Console(theme=rich_theme(theme))

    @property
    def console(self) -> Console:
        """Get the underlying Rich console."""
        return self._console

    def info(self, text: str) -> None:
        """Render informational text."""
        self._console.print(text, style="info")

    def muted(self, text: str) -> None:
        """Render secondary text."""
        self._console.print(text, style="muted")

    def link(self, text: str) -> None:
        """Render a line holding a link, never broken across lines."""
        # Rich breaks long lines with real newlines, which breaks a URL for
        # anyone copying it; soft wrapping leaves the wrapping to the terminal.
        self._console.print(text, style="info", soft_wrap=True)

    def warn(self, text: str) -> None:
        """Render warning text."""
        self._console.print(text, style="warn")

    def error(self, text: str) -> None:
        """Render error text."""
        self._console.print(text, style="error")

    def markdown(self, text: str) -> None:
        """Render Markdown-formatted text, with nothing drawn around it."""
        if not text.strip():
            return
        self._console.print(AnswerMarkdown(text))

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
        """Render a table."""
        table = Table(box=box.SIMPLE_HEAD)

        for header in headers:
            table.add_column(header, style="accent")

        for row in rows:
            table.add_row(*row)

        self._console.print(table)

    def clear(self) -> None:
        """Clear the terminal screen."""
        self._console.clear()

    def print(self, *args: Any, **kwargs: Any) -> None:
        """Print to console."""
        self._console.print(*args, **kwargs)


def _mark(view: RequestView) -> tuple[str, str]:
    """(mark, style name) for how a request ended."""
    if view.status == COMPLETED:
        return OK_MARK, "ok"
    if view.status == FAILED:
        return ERROR_MARK, "error"
    return CANCEL_MARK, "muted"


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


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
            reason = escape(clip(_first_line(call.result), 160))
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
            line += f" [error]failed: {escape(clip(view.error or '', 160))}[/error]"
        elif view.status == CANCELLED:
            line += " [muted]cancelled[/muted]"
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
        return tool_display(tool, tool_lines)
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
        tail = _first_line(view.tail.splitlines()[-1]) if view.tail.strip() else ""
        if tail:
            fragments.append(("class:live.muted", f"  {clip(tail, width - 4)}\n"))
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
