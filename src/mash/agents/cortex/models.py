"""Cortex records and settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_PROFILE_MAX_CHARS = 4000


@dataclass(frozen=True)
class CortexSettings:
    """What ``HostBuilder.enable_cortex()`` turns on for a pool."""

    profile_max_chars: int = DEFAULT_PROFILE_MAX_CHARS

    def __post_init__(self) -> None:
        if self.profile_max_chars <= 0:
            raise ValueError("profile_max_chars must be > 0")


@dataclass(frozen=True)
class CortexEntry:
    """One fact about a user: written by a build, or pinned by the app."""

    entry_id: str
    user_id: str
    topic: str
    content: str
    origin: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
