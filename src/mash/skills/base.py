"""Base tool protocol and result types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Skill:
    """Serializable skill definition used by the registry."""

    type: str
    name: str
    description: str = ""
    location: Optional[str] = None
