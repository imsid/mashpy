"""Ordered-file migration runner for the shared Mash Postgres schema.

The runtime event store, memory store, and evals store share one database
and one schema baseline. Each store calls run_migrations() on open.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

_MIGRATION_DIR = Path(__file__).parent

# Stores open concurrently against the same database; the advisory lock
# serializes their migration runs.
_ADVISORY_LOCK_KEY = 0x6D6173685F736368  # "mash_sch"


async def run_migrations(pool: Any) -> None:
    """Apply any pending .sql migrations from this directory in filename order.

    Applied migrations are recorded in _mash_migrations and skipped on later
    runs, so this is safe to call on every startup. The whole run happens in
    one transaction under a Postgres advisory lock, so concurrent store opens
    apply each migration exactly once.
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,)
                )
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
                    await cursor.execute(migration_file.read_text())
                    await cursor.execute(
                        "INSERT INTO _mash_migrations (name, applied_at) VALUES (%s, %s)",
                        (migration_file.name, time.time()),
                    )
