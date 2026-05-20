"""Configuration types for the Mash host API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from mash.core.database import resolve_database_url

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
    runtime_database_url: Optional[str] = None
    api_key: Optional[str] = None
    cors_allow_origins: Sequence[str] = field(default_factory=lambda: _DEFAULT_CORS_ORIGINS)
    enable_observability: bool = True
    default_events_limit: int = 2000
    default_search_limit: int = 10
    api_logging_enabled: bool = True
    api_log_api_only: bool = True
    api_log_body_enabled: bool = True
    api_log_body_max_bytes: int = 8192
    api_log_response_body_max_bytes: int = 2048
    api_log_excluded_paths: Sequence[str] = ("/api/v1/health",)
    api_log_redacted_headers: Sequence[str] = (
        "authorization",
        "x-api-key",
        "cookie",
        "set-cookie",
    )

    def resolved_api_key(self) -> Optional[str]:
        value = (self.api_key or "").strip()
        return value or None

    def resolved_runtime_database_url(self) -> Optional[str]:
        return resolve_database_url(self.runtime_database_url)

    def resolved_cors_origins(self) -> list[str]:
        values: list[str] = []
        for origin in self.cors_allow_origins:
            text = str(origin).strip()
            if text:
                values.append(text)
        return values
