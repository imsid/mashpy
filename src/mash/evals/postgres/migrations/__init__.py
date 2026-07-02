"""Ordered-file migration runner for the Postgres evals store."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

_MIGRATION_DIR = Path(__file__).parent


async def run_migrations(pool: Any) -> None:
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS _mash_migrations (
                        name       TEXT PRIMARY KEY,
                        applied_at DOUBLE PRECISION NOT NULL
                    )
                """)
                await cursor.execute("SELECT name FROM _mash_migrations")
                applied = {row["name"] for row in await cursor.fetchall()}

    for migration_file in sorted(_MIGRATION_DIR.glob("*.sql")):
        if migration_file.name in applied:
            continue
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(migration_file.read_text())
                    await cursor.execute(
                        "INSERT INTO _mash_migrations (name, applied_at) VALUES (%s, %s)",
                        (migration_file.name, time.time()),
                    )
