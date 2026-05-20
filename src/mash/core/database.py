"""Database URL resolution helpers."""

from __future__ import annotations

import os

MASH_DATABASE_URL_ENV = "MASH_DATABASE_URL"


def resolve_database_url(
    explicit_value: str | None = None,
) -> str | None:
    """Resolve Mash's shared Postgres database URL."""
    for candidate in (explicit_value, os.getenv(MASH_DATABASE_URL_ENV)):
        value = str(candidate or "").strip()
        if value:
            return value
    return None
