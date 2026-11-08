"""The process pool's size setting and the host's connection budget check."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import pytest

from mash.storage import pool as pool_module
from mash.storage.pool import (
    DEFAULT_MAX_SIZE,
    MASH_DB_POOL_MAX_SIZE_ENV,
    check_connection_budget,
    pool_max_size,
)


def test_default_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASH_DB_POOL_MAX_SIZE_ENV, raising=False)
    assert pool_max_size() == DEFAULT_MAX_SIZE == 10


def test_size_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MASH_DB_POOL_MAX_SIZE_ENV, "4")
    assert pool_max_size() == 4


@pytest.mark.parametrize("raw", ["0", "-3", "ten", "1.5"])
def test_bad_size_names_the_variable(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(MASH_DB_POOL_MAX_SIZE_ENV, raw)
    with pytest.raises(ValueError, match=MASH_DB_POOL_MAX_SIZE_ENV):
        pool_max_size()


def test_acquire_pool_uses_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, Any] = {}

    class _Pool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, url: str, **kwargs: Any) -> None:
            created.update(kwargs)

        async def open(self) -> None:
            return None

        async def close(self) -> None:
            return None

    async def _no_migrations(pool: Any) -> None:
        del pool

    monkeypatch.setenv(MASH_DB_POOL_MAX_SIZE_ENV, "3")
    monkeypatch.setattr(pool_module, "AsyncConnectionPool", _Pool)
    monkeypatch.setattr(pool_module, "run_migrations", _no_migrations)

    async def _run() -> None:
        await pool_module.acquire_pool("postgresql://size-test/db")
        await pool_module.release_pool("postgresql://size-test/db")

    asyncio.run(_run())
    assert created["max_size"] == 3


class _ServerPool:
    def __init__(self, max_connections: int) -> None:
        self.max_connections = max_connections

    @asynccontextmanager
    async def connection(self):
        server = self

        class _Cursor:
            async def execute(self, sql: str) -> None:
                assert sql == "SHOW max_connections"

            async def fetchone(self) -> dict[str, str]:
                return {"max_connections": str(server.max_connections)}

        class _Conn:
            @asynccontextmanager
            async def cursor(self):
                yield _Cursor()

        yield _Conn()


def test_budget_over_the_limit_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(MASH_DB_POOL_MAX_SIZE_ENV, "20")
    with caplog.at_level(logging.WARNING, logger="mash.storage.pool"):
        asyncio.run(check_connection_budget(_ServerPool(100), processes=7))
    assert "140 Postgres connections" in caplog.text
    assert "max_connections of 100" in caplog.text


def test_budget_under_the_limit_is_quiet(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(MASH_DB_POOL_MAX_SIZE_ENV, raising=False)
    with caplog.at_level(logging.WARNING, logger="mash.storage.pool"):
        asyncio.run(check_connection_budget(_ServerPool(100), processes=7))
    assert caplog.text == ""
