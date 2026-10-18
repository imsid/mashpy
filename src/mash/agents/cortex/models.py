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


class CortexConflictError(RuntimeError):
    """Another write landed on the user's Cortex since a build read it."""


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


@dataclass(frozen=True)
class CortexUserInfo:
    """A user's Cortex summary, as the app API reports it."""

    user_id: str
    profile: str
    built_through: float
    version: int
    entry_count: int
    updated_at: float


@dataclass(frozen=True)
class CortexSnapshot:
    """A user's Cortex as a build reads it. ``version`` is 0 for a new user."""

    user_id: str
    profile: str = ""
    built_through: float = 0.0
    version: int = 0
    entries: list[CortexEntry] = field(default_factory=list)


@dataclass(frozen=True)
class EntryChange:
    """One upsert from a build: ``entry_id`` None adds a new entry."""

    topic: str
    content: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    entry_id: str | None = None
