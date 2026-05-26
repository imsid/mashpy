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
    content: Optional[str] = None

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise ValueError("skill name is required")
        location = str(self.location or "").strip()
        content = str(self.content or "").strip()
        if not location and not content:
            raise ValueError("skill content or location is required")
