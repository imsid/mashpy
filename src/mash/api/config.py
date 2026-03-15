"""Configuration types for the Mash host API."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

_DEFAULT_CORS_ORIGINS = (
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)


@dataclass
class MashHostConfig:
    """Runtime configuration for Mash host server composition."""

    api_prefix: str = "/api/v1"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    runtime_bind_host: str = "127.0.0.1"
    api_key: Optional[str] = None
    cors_allow_origins: Sequence[str] = field(default_factory=lambda: _DEFAULT_CORS_ORIGINS)
    enable_observability: bool = True
    observability_memory_db_path: Optional[Path] = None
    default_events_limit: int = 2000
    default_search_limit: int = 10

    def resolved_api_key(self) -> Optional[str]:
        value = (self.api_key or "").strip()
        return value or None

    def resolved_cors_origins(self) -> list[str]:
        values: list[str] = []
        for origin in self.cors_allow_origins:
            text = str(origin).strip()
            if text:
                values.append(text)
        return values

    def resolved_memory_db_path(self) -> Optional[Path]:
        if self.observability_memory_db_path is None:
            return None
        return Path(self.observability_memory_db_path).expanduser().resolve()
