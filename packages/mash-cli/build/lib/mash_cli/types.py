"""CLI shell data types."""

from __future__ import annotations

from dataclasses import dataclass

from mash.runtime.client import MashAgentClient

from .render import RichRenderer


@dataclass
class CLIContext:
    """Context for CLI operations."""

    app_id: str
    session_id: str
    runtime: MashAgentClient
    renderer: RichRenderer
