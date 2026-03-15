"""CLI shell data types."""

from __future__ import annotations

from dataclasses import dataclass

from .render import RichRenderer
from .client import MashHostClient


@dataclass
class CLIContext:
    """Context for CLI operations."""

    api_base_url: str
    agent_id: str
    session_id: str
    client: MashHostClient
    renderer: RichRenderer
