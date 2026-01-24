"""Console rendering for CLI applications."""

from __future__ import annotations

from contextlib import contextmanager
from typing import ContextManager, List, Optional, Protocol

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.theme import Theme


class Renderer(Protocol):
    """Protocol for renderers."""

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

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
        """Render a table."""
        ...

    def status(self, message: str) -> ContextManager[object]:
        """Return a status spinner context manager."""
        ...

    def clear(self) -> None:
        """Clear the terminal screen."""
        ...


class RichRenderer:
    """Rich-based renderer for formatted CLI output."""

    def __init__(self, console: Optional[Console] = None) -> None:
        """Initialize renderer.

        Args:
            console: Rich console instance (creates new one if not provided).
        """
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
        """Get the underlying Rich console."""
        return self._console

    def info(self, text: str) -> None:
        """Render informational text."""
        self._console.print(text, style="info")

    def warn(self, text: str) -> None:
        """Render warning text."""
        self._console.print(text, style="warn")

    def error(self, text: str) -> None:
        """Render error text."""
        self._console.print(text, style="error")

    def markdown(self, text: str) -> None:
        """Render Markdown-formatted text."""
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

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
        """Render a table."""
        table = Table(box=box.ASCII)

        for header in headers:
            table.add_column(header, style="cyan")

        for row in rows:
            table.add_row(*row)

        self._console.print(table)

    @contextmanager
    def status(self, message: str) -> ContextManager[object]:
        """Return a status spinner context manager."""
        with self._console.status(message) as status:
            yield status

    def clear(self) -> None:
        """Clear the terminal screen."""
        self._console.clear()

    def print(self, *args: any, **kwargs: any) -> None:
        """Print to console."""
        self._console.print(*args, **kwargs)
