"""Console rendering helpers."""

from __future__ import annotations

from typing import ContextManager, List, Optional, Protocol

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.theme import Theme


class Renderer(Protocol):
    """Protocol describing the renderer surface."""

    def info(self, text: str) -> None:
        """Render informational text."""
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

    def code(self, text: str, lang: str = "") -> None:
        """Render a fenced code block."""
        ...

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
        """Render a simple table."""
        ...

    def status(self, message: str) -> ContextManager[object]:
        """Return a status spinner context manager."""
        ...

    def clear(self) -> None:
        """Clear the terminal screen."""
        ...


class RichRenderer(Renderer):
    """Renderer that uses Rich for formatted CLI output."""

    def __init__(self, console: Optional[Console] = None) -> None:
        theme = Theme(
            {
                "info": "bold cyan",
                "warn": "bold yellow",
                "error": "bold red",
                "muted": "dim",
            }
        )
        self._console = console or Console(theme=theme, soft_wrap=True)

    @property
    def console(self) -> Console:
        return self._console

    def info(self, text: str) -> None:
        self._console.print(text, style="info")

    def warn(self, text: str) -> None:
        self._console.print(text, style="warn")

    def error(self, text: str) -> None:
        self._console.print(text, style="error")

    def markdown(self, text: str) -> None:
        if not text.strip():
            return
        markdown = Markdown(text)
        panel = Panel(
            markdown,
            title="Assistant",
            border_style="cyan",
            box=box.ASCII,
            padding=(0, 1),
        )
        self._console.print(panel)

    def code(self, text: str, lang: str = "") -> None:
        syntax = Syntax(
            text,
            lang or "text",
            theme="monokai",
            line_numbers=True,
            word_wrap=False,
        )
        panel = Panel(
            syntax,
            title="Output",
            border_style="cyan",
            box=box.ASCII,
            padding=(0, 1),
        )
        self._console.print(panel)

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
        table = Table(show_header=True, header_style="bold cyan", box=box.ASCII)
        for header in headers:
            table.add_column(header)
        for row in rows:
            table.add_row(*[str(cell) for cell in row[: len(headers)]])
        self._console.print(table)

    def status(self, message: str) -> ContextManager[object]:
        return self._console.status(message, spinner="line")

    def clear(self) -> None:
        self._console.clear()
