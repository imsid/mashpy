"""Configuration types for the Mash host API."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from typing import Optional, Sequence

from mash.core.database import resolve_database_url
from mash.runtime.host.tokens import resolve_host_secret

MASH_PUBLIC_URL_ENV = "MASH_PUBLIC_URL"

_DEFAULT_CORS_ORIGINS = (
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)


def is_loopback_host(host: str) -> bool:
    """Whether ``host`` is an address only this machine can reach."""
    value = str(host or "").strip().lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value.strip("[]")).is_loopback
    except ValueError:
        return False


@dataclass
class MashHostConfig:
    """Runtime configuration for Mash host server composition."""

    api_prefix: str = "/api/v1"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    runtime_database_url: Optional[str] = None
    api_key: Optional[str] = None
    # Derives each agent's and each sandbox's token; falls back to MASH_HOST_SECRET.
    host_secret: Optional[str] = None
    # The ``module:attribute`` each agent process imports to serve its agent.
    host_app: Optional[str] = None
    # Where users reach the host, for sandbox links; falls back to MASH_PUBLIC_URL,
    # then to the bind address.
    public_url: Optional[str] = None
    cors_allow_origins: Sequence[str] = field(default_factory=lambda: _DEFAULT_CORS_ORIGINS)
    default_events_limit: int = 2000
    default_search_limit: int = 10

    def resolved_api_key(self) -> Optional[str]:
        value = (self.api_key or "").strip()
        return value or None

    def check_bind(self) -> None:
        """Refuse to serve the API on a non-loopback address without a key.

        Without a key ``/api/v1`` takes every request, so it may only listen
        where nothing but this machine can reach it.
        """
        if self.resolved_api_key() is None and not is_loopback_host(self.bind_host):
            raise ValueError(
                f"an API key is required to bind {self.bind_host!r}: set MASH_API_KEY "
                "(or --api-key), or bind 127.0.0.1"
            )

    def resolved_host_secret(self) -> Optional[str]:
        return resolve_host_secret(self.host_secret)

    def resolved_public_url(self) -> str:
        value = str(self.public_url or os.environ.get(MASH_PUBLIC_URL_ENV) or "").strip()
        if value:
            return value.rstrip("/")
        bind = self.bind_host if self.bind_host not in ("", "0.0.0.0", "::") else "127.0.0.1"
        return f"http://{bind}:{self.bind_port}"

    def resolved_runtime_database_url(self) -> Optional[str]:
        return resolve_database_url(self.runtime_database_url)

    def resolved_cors_origins(self) -> list[str]:
        values: list[str] = []
        for origin in self.cors_allow_origins:
            text = str(origin).strip()
            if text:
                values.append(text)
        return values
