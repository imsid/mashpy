"""Delete what finished before the retention window.

Off unless ``MASH_RETENTION_DAYS`` is set. The host then runs :func:`purge`
when it starts and every hour after. Each pass deletes, in batches so no
statement holds locks for long:

- finished requests (not ``running``) last updated before the window, with
  their events and their seq counter;
- events that belong to no request, created before the window;
- finished workflow runs before the window, with their steps and step
  events, unless an eval experiment points at the run;
- checkpoint images (``runtime_blob``) that no remaining request references
  and no checkpoint save used within the window.

Conversation turns (``memory_turns``), Cortex, feedback and evals are kept.
A deleted request can no longer be resumed, rerun or shown in traces.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from .pool import acquire_pool, release_pool

MASH_RETENTION_DAYS_ENV = "MASH_RETENTION_DAYS"
PURGE_INTERVAL_SECONDS = 3600.0
# Requests and runs per statement; each takes all of its events with it.
OWNER_BATCH = 100
# Rows per statement for events without a request, and for blobs.
ROW_BATCH = 1000

logger = logging.getLogger(__name__)


def retention_days() -> float | None:
    """Days to keep, from ``MASH_RETENTION_DAYS``; None keeps everything."""
    raw = (os.environ.get(MASH_RETENTION_DAYS_ENV) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not value > 0:
        raise ValueError(
            f"{MASH_RETENTION_DAYS_ENV} must be a positive number of days, got {raw!r}"
        )
    return value


# The requests are locked first and skipped when another transaction holds
# them, such as a resume in progress. The outer DELETE repeats the filter, so
# a row changed while this waited is checked again.
_PURGE_REQUESTS = """
    WITH expired AS (
        SELECT request_id FROM runtime_request
        WHERE status <> 'running' AND updated_at < %(cutoff)s
        ORDER BY updated_at
        LIMIT %(batch)s
        FOR UPDATE SKIP LOCKED
    ),
    events AS (
        DELETE FROM runtime_event_log
        WHERE request_id IN (SELECT request_id FROM expired)
    ),
    counters AS (
        DELETE FROM runtime_request_seq
        WHERE request_id IN (SELECT request_id FROM expired)
    )
    DELETE FROM runtime_request
    WHERE request_id IN (SELECT request_id FROM expired)
      AND status <> 'running' AND updated_at < %(cutoff)s
    RETURNING request_id
"""

_PURGE_UNOWNED_EVENTS = """
    DELETE FROM runtime_event_log
    WHERE event_id IN (
        SELECT event_id FROM runtime_event_log
        WHERE request_id IS NULL AND created_at < %(cutoff)s
        ORDER BY created_at
        LIMIT %(batch)s
    )
    RETURNING event_id
"""

_PURGE_WORKFLOW_RUNS = """
    WITH expired AS (
        SELECT run_id FROM workflow_runs AS run
        WHERE status IN ('completed', 'failed', 'cancelled')
          AND finished_at < %(cutoff)s
          AND NOT EXISTS (
              SELECT 1 FROM eval_experiment
              WHERE eval_experiment.workflow_run_id = run.run_id
          )
        ORDER BY finished_at
        LIMIT %(batch)s
        FOR UPDATE SKIP LOCKED
    ),
    events AS (
        DELETE FROM workflow_step_events
        WHERE run_id IN (SELECT run_id FROM expired)
    )
    DELETE FROM workflow_runs
    WHERE run_id IN (SELECT run_id FROM expired)
      AND status IN ('completed', 'failed', 'cancelled')
      AND finished_at < %(cutoff)s
    RETURNING run_id
"""

# A checkpoint save bumps last_used_at before it writes the reference, and the
# outer filter is checked again against a row a save just bumped, so a blob a
# save is about to use is never deleted.
_PURGE_BLOBS = """
    DELETE FROM runtime_blob
    WHERE last_used_at < %(cutoff)s
      AND sha256 IN (
          SELECT blob.sha256 FROM runtime_blob AS blob
          WHERE blob.last_used_at < %(cutoff)s
            AND NOT EXISTS (
                SELECT 1 FROM runtime_request
                WHERE runtime_request.blob_refs @> ARRAY[blob.sha256]
            )
          LIMIT %(batch)s
      )
    RETURNING sha256
"""


async def _drain(pool: Any, sql: str, *, cutoff: float, batch: int) -> int:
    """Run one batched delete until a batch comes back short; rows deleted."""
    total = 0
    while True:
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, {"cutoff": cutoff, "batch": batch})
                deleted = len(await cursor.fetchall())
        total += deleted
        if deleted < batch:
            return total


async def purge(pool: Any, *, cutoff: float) -> dict[str, int]:
    """Delete what finished before ``cutoff`` (epoch seconds); counts deleted."""
    counts = {
        "requests": await _drain(pool, _PURGE_REQUESTS, cutoff=cutoff, batch=OWNER_BATCH),
        "unowned_events": await _drain(
            pool, _PURGE_UNOWNED_EVENTS, cutoff=cutoff, batch=ROW_BATCH
        ),
        "workflow_runs": await _drain(
            pool, _PURGE_WORKFLOW_RUNS, cutoff=cutoff, batch=OWNER_BATCH
        ),
        # Last: the requests deleted above no longer hold their blobs.
        "blobs": await _drain(pool, _PURGE_BLOBS, cutoff=cutoff, batch=ROW_BATCH),
    }
    return counts


class Retention:
    """Runs :func:`purge` for ``days`` when started, then every hour."""

    def __init__(
        self,
        database_url: str,
        *,
        days: float,
        interval_seconds: float = PURGE_INTERVAL_SECONDS,
    ) -> None:
        self._database_url = database_url
        self.days = days
        self._interval = interval_seconds
        self._pool: Any = None
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_env(cls, database_url: str) -> Retention | None:
        days = retention_days()
        if days is None or not database_url:
            return None
        return cls(database_url, days=days)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._pool = await acquire_pool(self._database_url)
        self._task = asyncio.create_task(self._run(), name="mash-retention")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._pool is not None:
            self._pool = None
            await release_pool(self._database_url)

    async def purge_once(self) -> dict[str, int]:
        cutoff = time.time() - self.days * 86400.0
        counts = await purge(self._pool, cutoff=cutoff)
        if any(counts.values()):
            logger.info(
                "retention (%g days) deleted %d requests, %d events without a "
                "request, %d workflow runs and %d images",
                self.days,
                counts["requests"],
                counts["unowned_events"],
                counts["workflow_runs"],
                counts["blobs"],
            )
        return counts

    async def _run(self) -> None:
        while True:
            try:
                await self.purge_once()
            except Exception:  # pragma: no cover - logged, retried next pass
                logger.exception("retention pass failed; retrying in %gs", self._interval)
            await asyncio.sleep(self._interval)


__all__ = [
    "MASH_RETENTION_DAYS_ENV",
    "Retention",
    "purge",
    "retention_days",
]
