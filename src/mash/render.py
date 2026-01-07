"""Console rendering helpers."""

from __future__ import annotations

from typing import List, Protocol


class Renderer(Protocol):
    """Protocol describing the renderer surface."""

    def info(self, text: str) -> None:
        """Render informational text."""

    def warn(self, text: str) -> None:
        """Render warning text."""

    def error(self, text: str) -> None:
        """Render error text."""

    def markdown(self, text: str) -> None:
        """Render Markdown-formatted text."""

    def code(self, text: str, lang: str = "") -> None:
        """Render a fenced code block."""

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
        """Render a simple table."""


class PlainRenderer(Renderer):
    """Renderer that prints directly to stdout."""

    def info(self, text: str) -> None:
        """Render informational text."""

        print(text)

    def warn(self, text: str) -> None:
        """Render warning text."""

        print(f"[warn] {text}")

    def error(self, text: str) -> None:
        """Render error text."""

        print(f"[error] {text}")

    def markdown(self, text: str) -> None:
        """Render Markdown text without extra formatting."""

        print(text)

    def code(self, text: str, lang: str = "") -> None:
        """Render a fenced code block."""

        fence = f"```{lang}" if lang else "```"
        print(fence)
        print(text)
        print("```")

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
        """Render a plain-text table."""

        widths = [len(header) for header in headers]
        for row in rows:
            for idx, cell in enumerate(row[: len(headers)]):
                widths[idx] = max(widths[idx], len(cell))
        divider = "-+-".join("-" * width for width in widths)

        def _render_row(cells: List[str]) -> None:
            padded = []
            for idx in range(len(headers)):
                cell = cells[idx] if idx < len(cells) else ""
                padded.append(cell.ljust(widths[idx]))
            print(" | ".join(padded))

        _render_row(headers)
        print(divider)
        for row in rows:
            _render_row(row)
