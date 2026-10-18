"""The Cortex API: inspecting and changing a user's Cortex over the host routes."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

from fastapi.testclient import TestClient

from mash.agents.cortex import CortexEntry
from mash.agents.cortex.routes import cortex_context
from mash.api import create_app
from mash.api.config import MashHostConfig
from mash.runtime import HostBuilder
from mash.testing.runtime_fixtures import build_spec, metadata


@contextmanager
def _client(*, cortex: bool = True) -> Iterator[tuple[TestClient, object]]:
    builder = HostBuilder().agent(build_spec(agent_id="assistant"), metadata=metadata())
    pool = (builder.enable_cortex() if cortex else builder).build()
    config = MashHostConfig(
        runtime_database_url="postgresql://test/runtime", api_logging_enabled=False
    )
    # The pool's built-in agents build their LLM providers at startup.
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with TestClient(create_app(pool, config=config)) as client:
            yield client, pool


def _store(pool):
    return cortex_context(pool).require_store()


def test_routes_exist_only_with_cortex() -> None:
    with _client(cortex=False) as (client, _):
        assert client.get("/api/v1/cortex/users/u-1").status_code == 404


def test_app_entries_are_created_listed_edited_and_deleted() -> None:
    with _client() as (client, pool):
        assert client.get("/api/v1/cortex/users/u-1").json()["error"]["code"] == "CORTEX_NOT_FOUND"

        created = client.post(
            "/api/v1/cortex/users/u-1/entries", json={"topic": "plan", "content": "Enterprise plan"}
        )
        assert created.status_code == 200
        entry = created.json()["data"]["entry"]
        assert entry["origin"] == "app"
        assert created.json()["data"]["version"] == 1

        info = client.get("/api/v1/cortex/users/u-1").json()["data"]
        assert (info["entry_count"], info["version"], info["profile"]) == (1, 1, "")

        patched = client.patch(
            f"/api/v1/cortex/users/u-1/entries/{entry['entry_id']}", json={"content": "Free plan"}
        )
        assert patched.json()["data"]["entry"]["content"] == "Free plan"
        assert patched.json()["data"]["entry"]["topic"] == "plan"
        assert patched.json()["data"]["version"] == 2

        found = client.get("/api/v1/cortex/users/u-1/entries", params={"q": "free"})
        assert [e["entry_id"] for e in found.json()["data"]["entries"]] == [entry["entry_id"]]

        deleted = client.delete(f"/api/v1/cortex/users/u-1/entries/{entry['entry_id']}")
        assert deleted.json()["data"]["version"] == 3
        missing = client.delete(f"/api/v1/cortex/users/u-1/entries/{entry['entry_id']}")
        assert missing.status_code == 404


def test_editing_a_build_entry_pins_it() -> None:
    with _client() as (client, pool):
        mem = _store(pool)._mem
        mem.profiles["u-1"] = "Uses imperial."
        mem.entries["e-1"] = CortexEntry(
            entry_id="e-1", user_id="u-1", topic="units", content="Uses imperial",
            origin="build", updated_at=1.0,
        )
        mem.entries["e-2"] = CortexEntry(
            entry_id="e-2", user_id="u-2", topic="units", content="Other user",
            origin="build", updated_at=2.0,
        )

        builds = client.get("/api/v1/cortex/users/u-1/entries", params={"origin": "build"})
        assert [e["entry_id"] for e in builds.json()["data"]["entries"]] == ["e-1"]

        client.patch("/api/v1/cortex/users/u-1/entries/e-1", json={"content": "Uses metric"})
        assert mem.entries["e-1"].origin == "app"

        # Another user's entry is out of reach.
        assert client.patch(
            "/api/v1/cortex/users/u-1/entries/e-2", json={"content": "x"}
        ).status_code == 404


def test_entries_page_newest_first() -> None:
    with _client() as (client, pool):
        mem = _store(pool)._mem
        mem.profiles["u-1"] = ""
        for index in range(3):
            entry_id = f"e-{index}"
            mem.entries[entry_id] = CortexEntry(
                entry_id=entry_id, user_id="u-1", topic="t", content=f"fact {index}",
                origin="build", updated_at=float(index),
            )

        first = client.get("/api/v1/cortex/users/u-1/entries", params={"limit": 2}).json()["data"]
        assert [e["entry_id"] for e in first["entries"]] == ["e-2", "e-1"]
        rest = client.get(
            "/api/v1/cortex/users/u-1/entries", params={"limit": 2, "after": first["next"]}
        ).json()["data"]
        assert [e["entry_id"] for e in rest["entries"]] == ["e-0"]
        assert rest["next"] is None


def test_deleting_a_user_removes_everything() -> None:
    with _client() as (client, pool):
        client.post("/api/v1/cortex/users/u-1/entries", json={"topic": "a", "content": "b"})

        assert client.delete("/api/v1/cortex/users/u-1").json()["data"] == {"deleted": "u-1"}
        assert client.get("/api/v1/cortex/users/u-1").status_code == 404
        assert client.delete("/api/v1/cortex/users/u-1").status_code == 404


def test_invalid_input_is_rejected() -> None:
    with _client() as (client, _):
        assert client.get("/api/v1/cortex/users/has%20space").json()["error"]["code"] == "INVALID_USER_ID"
        assert client.post(
            "/api/v1/cortex/users/u-1/entries", json={"topic": "", "content": "b"}
        ).status_code == 422
        assert client.patch("/api/v1/cortex/users/u-1/entries/e-1", json={}).status_code == 400
        assert client.get(
            "/api/v1/cortex/users/u-1/entries", params={"origin": "other"}
        ).status_code == 422


def test_postgres_app_writes_bump_the_version_a_build_checks() -> None:
    import asyncio

    import pytest

    from mash.agents.cortex import CortexConflictError, CortexStore

    psycopg = pytest.importorskip("psycopg")
    url = os.environ.get(
        "MASH_REAL_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/mash"
    )
    try:
        psycopg.connect(url, autocommit=True).close()
    except Exception as exc:  # pragma: no cover - depends on local postgres
        pytest.skip(f"Postgres unavailable: {exc}")
    user_id = f"cortex-api-{uuid.uuid4().hex}"

    async def scenario() -> None:
        store = CortexStore(url)
        await store.open()
        try:
            entry, version = await store.create_entry(user_id, topic="plan", content="Enterprise")
            assert (entry.origin, version) == ("app", 1)
            older, _ = await store.create_entry(user_id, topic="tz", content="Berlin")

            updated, version = await store.update_entry(user_id, entry.entry_id, content="Free")
            assert (updated.content, updated.topic, version) == ("Free", "plan", 3)
            assert await store.update_entry(user_id, "missing", content="x") is None

            page = await store.list_entries(user_id, limit=1)
            assert [e.entry_id for e in page] == [entry.entry_id]
            rest = await store.list_entries(user_id, limit=1, after=page[0].entry_id)
            assert [e.entry_id for e in rest] == [older.entry_id]
            assert [e.content for e in await store.list_entries(user_id, q="berlin")] == ["Berlin"]

            info = await store.get_user(user_id)
            assert (info.version, info.entry_count) == (3, 2)

            # A build that read version 3 fails once the app writes again.
            assert await store.delete_entry(user_id, older.entry_id) == 4
            try:
                await store.apply_build(
                    user_id=user_id, run_id="run-1", base_version=3, built_through=1.0,
                    profile="p", upserts=[], deletes=[], result={},
                )
            except CortexConflictError:
                pass
            else:  # pragma: no cover
                raise AssertionError("expected a version conflict")

            assert await store.delete_user(user_id) is True
            assert await store.get_user(user_id) is None
            assert await store.delete_user(user_id) is False
        finally:
            await store.close()

    asyncio.run(scenario())
